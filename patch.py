from curl_cffi import requests 

def request(method, url, **kwargs):
    session = requests.Session(impersonate="chrome110")
    session.headers.update(get_headers())

    if not kwargs.get('proxies', None):
        kwargs['proxies'] = {
            'https': constant.CONFIG['proxy'],
            'http': constant.CONFIG['proxy'],
        }

    return getattr(session, method)(url, verify=False, **kwargs)