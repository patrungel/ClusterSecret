import kopf
import re
from kubernetes import client

def patch_clustersecret_status(logger, namespace, name, new_status, v1=None):
    """Patch the status of a given clustersecret object
    """
    v1 = v1 or client.CustomObjectsApi()

    group = 'clustersecret.io'
    version = 'v1'
    plural = 'clustersecrets'

    # Retrieve the clustersecret object
    clustersecret = v1.get_namespaced_custom_object(
        group=group,
        version=version,
        namespace=namespace,
        plural=plural,
        name=name
    )

    # Update the status field
    clustersecret['status'] = new_status
    logger.debug(f'Updated clustersecret manifest: {clustersecret}')

    # Perform a patch operation to update the custom resource
    v1.patch_namespaced_custom_object(
        group=group,
        version=version,
        namespace=namespace,
        plural=plural,
        name=name,
        body=clustersecret
    )

def get_ns_list(logger,body,v1=None):
    """Returns a list of namespaces where the secret should be matched
    """
    if v1 is None:
        v1 = client.CoreV1Api()
        logger.debug('new client - fn get_ns_list')
    
    try:
        matchNamespace = body.get('matchNamespace')
    except KeyError:
        matchNamespace = '*'
        logger.debug("matching all namespaces.")
    logger.debug(f'Matching namespaces: {matchNamespace}')
    
    if matchNamespace is None:  # if delted key (issue 26)
        matchNamespace = '*'
    
    try:
        avoidNamespaces = body.get('avoidNamespaces')
    except KeyError:
        avoidNamespaces = ''
        logger.debug("not avoiding namespaces")

    nss = v1.list_namespace().items
    matchedns = []
    avoidedns = []

    for matchns in matchNamespace:
        for ns in nss:
            if re.match(matchns, ns.metadata.name):
                matchedns.append(ns.metadata.name)
                logger.debug(f'Matched namespaces: {ns.metadata.name} match pattern: {matchns}')
    if avoidNamespaces:
        for avoidns in avoidNamespaces:
            for ns in nss:
                if re.match(avoidns, ns.metadata.name):
                    avoidedns.append(ns.metadata.name)
                    logger.debug(f'Skipping namespaces: {ns.metadata.name} avoid pattern: {avoidns}')  
    # purge
    for ns in matchedns.copy():
        if ns in avoidedns:
            matchedns.remove(ns)

    return matchedns

def read_data_secret(logger,name,namespace,v1):
    """Gets the data from the 'name' secret in namespace
    """
    data={}
    logger.debug(f'Reading {name} from ns {namespace}')
    try: 
        secret = v1.read_namespaced_secret(name, namespace)
        logger.debug(f'Obtained secret {secret}')
        data = secret.data
    except client.exceptions.ApiException as e:
        logger.error('Error reading secret')
        logger.debug(f'error: {e}')
        if e == "404":
            logger.error(f"Secret {name} in ns {namespace} not found!")
        raise kopf.TemporaryError("Error reading secret")
    return data

def delete_secret(logger,namespace,name,v1=None):
    """Deletes a given secret from a given namespace
    """
    v1 = v1 or client.CoreV1Api()

    logger.info(f'deleting secret {name} from namespace {namespace}')
    try:
        v1.delete_namespaced_secret(name,namespace)
    except client.rest.ApiException as e:
        if e.status == 404:
            logger.warning(f"The namespace {namespace} may not exist anymore: Not found")
        else:
            logger.warning(" Something weird deleting the secret")
            logger.debug(f"details: {e}")

def create_secret(logger,namespace,body,v1=None):
    """Creates a given secret on a given namespace
    """
    if v1 is None:
        v1 = client.CoreV1Api()
        logger.debug('new client - fn create secret')
    try:
        sec_name = body['metadata']['name']
    except KeyError:
        logger.debug("No name in body ?")
        raise kopf.TemporaryError("can not get the name.")
    data = body.get('data') or dict()

    if 'valueFrom' in data:
        if len(data.keys()) > 1:
            logger.error('Data keys with ValueFrom error, enable debug for more details')
            logger.debug(f'keys: {data.keys()}  len {len(data.keys())}')
            raise kopf.TemporaryError("ValueFrom can not coexist with other keys in the data")
            
        try:
            ns_from = data['valueFrom']['secretKeyRef']['namespace']
            name_from = data['valueFrom']['secretKeyRef']['name']
            # key_from = data['ValueFrom']['secretKeyRef']['name']
            # to-do specifie keys. for now. it will clone all.
            logger.debug(f'Taking value from secret {name_from} from namespace {ns_from} - All keys')
            data = read_data_secret(logger,name_from,ns_from,v1)
        except KeyError:
            logger.error (f'ERROR reading data from remote secret, enable debug for more details')
            logger.debug (f'Deta details: {data}')
            raise kopf.TemporaryError("Can not get Values from external secret")

    string_data = body.get('stringData') or dict()
    logger.debug(f'Going to create with data: {data} and string_data: {string_data}')
    secret_type = 'Opaque'
    if 'type' in body:
        secret_type = body['type']
    body  = client.V1Secret()
    body.metadata = client.V1ObjectMeta(name=sec_name)
    body.type = secret_type
    body.data = data
    # kopf.adopt(body)
    body.string_data = string_data
    logger.info(f"cloning secret in namespace {namespace}")
    try:
        api_response = v1.create_namespaced_secret(namespace, body)
    except client.rest.ApiException as e:
        if e.reason == 'Conflict':
            logger.info(f"secret `{sec_name}` already exist in namespace '{namespace}'")
            return 0
        logger.error(f'Can not create a secret, it is base64 encoded? enable debug for details')
        logger.debug(f'data: {data}')
        logger.debug(f'Kube exception {e}')
        return 1
    return 0

def update_secret_data(data, string_data, name, obj_body, logger):
    logger.debug(f'Updating Object body == {obj_body}')

    try:
        syncedns = obj_body['status']['create_fn']['syncedns']
    except KeyError:
        logger.error('No Synced or status Namespaces found')
        syncedns = []

    v1 = client.CoreV1Api()

    secret_type = 'Opaque'
    if 'type' in obj_body:
        secret_type = obj_body['type']

    for ns in syncedns:
        logger.info(f'Re Syncing secret {name} in ns {ns}')
        metadata = {'name': name, 'namespace': ns}
        api_version = 'v1'
        kind = 'Secret'
        obj_body = client.V1Secret(
            api_version=api_version,
            data=data,
            string_data=string_data,
            kind=kind,
            metadata=metadata,
            type=secret_type
        )
        response = v1.replace_namespaced_secret(name, ns, obj_body)
        logger.debug(response)
