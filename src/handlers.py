import kopf
from kubernetes import client, config
from csHelper import *

csecs = {} # all cluster secrets.

@kopf.on.delete('clustersecret.io', 'v2beta1', 'clustersecrets')
def on_delete(spec,uid,body,name,logger=None, **_):
    try:
        syncedns = body['status']['create_fn']['syncedns']
    except KeyError:
        syncedns=[]
    v1 = client.CoreV1Api()
    for ns in syncedns:
        logger.info(f'deleting secret {name} from namespace {ns}')
        delete_secret(logger, ns, name, v1)
        
    #delete also from memory: prevent syncing with new namespaces
    try:
        csecs.pop(uid)
        logger.debug(f"csec {uid} deleted from memory ok")
    except KeyError as k:
        logger.info(f" This csec were not found in memory, maybe it was created in another run: {k}")

@kopf.on.field('clustersecret.io', 'v2beta1', 'clustersecrets', field='matchNamespace')
def on_field_match_namespace(old, new, name, namespace, body, uid, logger=None, **_):
    logger.debug(f'Namespaces changed: {old} -> {new}')

    if old is not None:
        logger.debug(f'Updating Object body == {body}')

        try:
            syncedns = body['status']['create_fn']['syncedns']
        except KeyError:
            logger.error('No Synced or status Namespaces found')
            syncedns = []

        v1 = client.CoreV1Api()
        updated_matched = get_ns_list(logger, body, v1)
        to_add = set(updated_matched).difference(set(syncedns))
        to_remove = set(syncedns).difference(set(updated_matched))

        logger.debug(f'Add secret to namespaces: {to_add}, remove from: {to_remove}')

        for secret_namespace in to_add:
            create_secret(logger, secret_namespace, body)
        for secret_namespace in to_remove:
            delete_secret(logger, secret_namespace, name)

        # Store status in memory
        csecs[uid] = {
            'body': body,
            'syncedns': updated_matched
        }

        # Patch synced_ns field
        logger.debug(f'Patching clustersecret {name} in namespace {namespace}')
        patch_clustersecret_status(logger, namespace, name, {'create_fn': {'syncedns': updated_matched}})
    else:
        logger.debug('This is a new object')


@kopf.on.field('clustersecret.io', 'v2beta1', 'clustersecrets', field='data')
def on_field_data(old, new, body,name,logger=None, **_):
    logger.debug(f'Data changed: {old} -> {new}')
    if old is not None:
        update_secret_data(new, body.get('stringData'), name, body, logger)
    else:
        logger.debug('This is a new object')

@kopf.on.field('clustersecret.io', 'v2beta1', 'clustersecrets', field='stringData')
def on_field_data(old, new, body,name,logger=None, **_):
    logger.debug(f'StringData changed: {old} -> {new}')
    if old is not None:
        update_secret_data(body.get('data'), new, name, body, logger)
    else:
        logger.debug('This is a new object')

@kopf.on.resume('clustersecret.io', 'v2beta1', 'clustersecrets')
@kopf.on.create('clustersecret.io', 'v2beta1', 'clustersecrets')
async def create_fn(spec,uid,logger=None,body=None,**kwargs):
    v1 = client.CoreV1Api()
    
    # warning this is debug!
    logger.debug("""
      #########################################################################
      # DEBUG MODE ON - NOT FOR PRODUCTION                                    #
      # On this mode secrets are leaked to stdout, this is not safe!. NO-GO ! #
      #########################################################################
    """
    )
    
    #get all ns matching.
    matchedns = get_ns_list(logger,body,v1)
        
    #sync in all matched NS
    logger.info(f'Syncing on Namespaces: {matchedns}')
    for namespace in matchedns:
        create_secret(logger,namespace,body,v1)
    
    #store status in memory
    csecs[uid]={}
    csecs[uid]['body']=body
    csecs[uid]['syncedns']=matchedns

    return {'syncedns': matchedns}

@kopf.on.create('', 'v1', 'namespaces')
async def namespace_watcher(spec,patch,logger,meta,body,**kwargs):
    """Watch for namespace events
    """
    new_ns = meta['name']
    logger.debug(f"New namespace created: {new_ns} re-syncing")
    v1 = client.CoreV1Api()
    ns_new_list = []
    for k,v in csecs.items():
        obj_body = v['body']
        #logger.debug(f'k: {k} \n v:{v}')
        matcheddns = v['syncedns']
        logger.debug(f"Old matched namespace: {matcheddns} - name: {v['body']['metadata']['name']}")
        ns_new_list=get_ns_list(logger,obj_body,v1)
        logger.debug(f"new matched list: {ns_new_list}")
        if new_ns in ns_new_list:
            logger.debug(f"Cloning secret {v['body']['metadata']['name']} into the new namespace {new_ns}")
            create_secret(logger,new_ns,v['body'],v1)
            # if there is a new matching ns, refresh memory
            v['syncedns'] = ns_new_list
            
    # update ns_new_list on the object so then we also delete from there
    return {'syncedns': ns_new_list}
