# A refused routes read stops the capture

Status: accepted (owner, 2026-10-22); narrows 0009

A 403 on `GET /subnets` or `GET /subnets/all` stops the capture, and so `build` and `compare`, instead of being reported as not readable: without them the preconfig would lose the site's routes, and a site that comes up without its routes is worse than no file. Every other refused read is still reported, per 0009.

Checked by: `tests/test_capture.py`.
