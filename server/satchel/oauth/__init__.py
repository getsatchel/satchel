"""OAuth 2.1 authorization server + OIDC login (design §9).

Satchel is both the MCP protected resource and its own authorization server; user login is
delegated to the workspace's OIDC IdP. Modules:

- ``common``   hashing, URLs/resource URIs, redirect-URI rules
- ``clients``  dynamic client registration (RFC 7591) and Client ID Metadata Documents
- ``oidc``     the IdP client (discovery, code exchange, ID-token validation)
- ``flows``    authorize -> IdP -> workspace -> consent -> code, and the human web login
- ``tokens``   code exchange, refresh rotation with reuse detection, revocation
- ``sessions`` browser sessions (cookie) for the REST API
- ``routes``   the Starlette routes that expose all of the above
"""
