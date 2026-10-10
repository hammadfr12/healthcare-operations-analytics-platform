"""HTTP layer.

The routers here are deliberately thin: they parse a request, delegate to the
functions in :mod:`app.analytics` / :mod:`app.metrics`, and serialise the
result. No business logic lives in this package, which is what keeps the web
API, the CSV exports and the Tableau extract reporting identical numbers.
"""
