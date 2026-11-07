"""The student discussion UI: a Vektra-origin page shown in a cross-origin iframe (ARCH-076).

``router`` serves the page (``GET /api/v1/assess/ui?site=<site id>``) and its built assets
(``/api/v1/assess/ui/assets/{name}``, manifest allowlist only); ``headers`` builds the response
headers (strict CSP with the site's single ``frame-ancestors`` origin). The client bundle is
built from ``vektra-assess/ui/`` into ``static/`` (Docker node stage), never committed.
"""
