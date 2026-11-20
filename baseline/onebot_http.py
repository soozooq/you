"""Bounded HTTP JSON requests for the local OneBot endpoints."""
import json
import urllib.error
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        raise urllib.error.HTTPError(request.full_url, code, "OneBot redirect blocked",
                                    headers, response)


def read_json(request, *, timeout, limit):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            if not 200 <= response.status < 300:
                raise ValueError("Unexpected OneBot response status")
            body = response.read(limit + 1)
            if len(body) > limit:
                raise ValueError("OneBot response exceeded byte limit")
        return json.loads(body)
    except urllib.error.HTTPError as error:
        error.close()
        raise ValueError(f"OneBot HTTP status {error.code}") from None
