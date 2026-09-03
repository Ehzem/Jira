# v3.17 All Work column transport fix

v3.16 stopped after the first experimental `Content-Type: */*` request returned HTTP 500, so the documented multipart request was never attempted.

v3.17 removes that invalid first attempt and tries every transport independently. It starts with a true `requests` multipart request containing repeated `columns` parts and `X-Atlassian-Token: no-check`, then tries array-style multipart, the exact repeated `curl -d columns=...` form shape, JSON, and a safe query-string probe. REST v3 and v2 are both attempted.

A transport is not considered a successful All Work restore by the importer until the normal GET read-back exactly equals the requested mapped column order.
