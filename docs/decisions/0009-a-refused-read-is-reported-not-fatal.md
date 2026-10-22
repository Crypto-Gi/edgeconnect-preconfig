# A read the API key may not make is reported, not fatal

Status: accepted (owner, 2026-10-22)

Orchestrator RBAC (9.3 and later) limits an API key to the endpoints behind its roles' menus, and filters the appliance passthrough (`/appliance/rest`) by the URL inside it. On the SE team's lab Orchestrator a key with every menu the 10.0 UI offers was still refused `subnets3/configured`. Capture used to stop on any 403; now a 403 on one read is recorded as `denied` and the capture goes on, and translate reports every denied read as `not-readable` ("check by hand") with the sections it feeds, so the verdict counts it and it is never silent. A refused appliance listing, a 401, a redirect, a TLS or a network error still stop the capture.

Considered: a second, unrestricted read-only key for capture (more setup for every user); finding the menu behind each refused read (not documented, guesswork).

Checked by: `tests/test_capture.py`, `tests/test_report.py`.
