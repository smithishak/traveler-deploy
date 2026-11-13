"""Provision the panel's internal, destination-limited diagnostic credential."""

import hashlib
import secrets


DESTINATIONS = ('example.com', 'api.ipify.org')


def probe_name(password):
    suffix = hashlib.sha256(password.encode('utf-8')).hexdigest()[:24]
    return f'traveler-probe-{suffix}'


def is_probe(credential):
    password = credential.get('password')
    return (isinstance(password, str) and bool(password)
            and credential.get('name') == probe_name(password))


def ensure_probe(config, inbound):
    probes = [credential for credential in inbound['users'] if is_probe(credential)]
    if len(probes) > 1:
        raise ValueError('More than one diagnostic credential')
    if probes:
        credential = probes[0]
    else:
        password = secrets.token_urlsafe(32)
        credential = {'name': probe_name(password), 'password': password}
        inbound['users'].append(credential)

    route = config.setdefault('route', {})
    direct = next((item for item in config['outbounds']
                   if item['type'] == 'direct' and item.get('tag')), None)
    if direct is None:
        direct = {'type': 'direct', 'tag': 'traveler-probe-direct'}
        config['outbounds'].append(direct)
    match = {'auth_user': [credential['name']]}
    rules = [
        {**match, 'network': 'tcp', 'domain': list(DESTINATIONS), 'port': 443,
         'action': 'route', 'outbound': direct['tag']},
        {**match, 'action': 'reject'},
    ]
    # Remove the previous managed pair before prepending it. Other routing rules
    # retain their order, and no ordinary credential matches this pair.
    previous = route.get('rules', [])
    route['rules'] = rules + [rule for rule in previous if rule.get('auth_user') != match['auth_user']]
    return credential
