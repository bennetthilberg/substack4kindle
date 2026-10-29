# Persistent manually saved articles

Status: Validated

User request: keep the Felix article visible in Saved after every sync, with the date October 28, 2026 (America/Chicago). Updating the existing deployment is authorized by that request.

Recipe: AZCLI, application source update only. Reuse substack-kindle in substack-kindle-rg, subscription 1c07315e-9958-4d4f-a090-f0819cd42aac, East US. No infrastructure, identity, permissions, image, capacity, networking, or credential changes.

## Implementation

Store manual articles in /mnt/data/manual-articles.json, outside the replaced feed cache. Merge them first in Saved, deduplicate IDs, and resolve their full bodies independently of the cache. Preserve the original publication date separately; display the requested added date. Back up before seeding the manual record from the existing article cache.

## Deployment

Inspect and reuse the existing SOURCE_URL archive deployment. Package only app and requirements.txt, upload an immutable archive beside the existing source archive, and update SOURCE_URL and DEPLOY_VERSION on the existing container app. Retain the previous source URL and revision for rollback.

## Validation

- [x] All validation checks pass
  - CLI/auth and exact existing app/subscription confirmed.
  - Repeated feed syncs, empty Saved syncs, ID deduplication, independent full body retrieval, and authenticated reader rendering tested with isolated fixture data.
  - Python compilation and source archive contents verified.
  - No infrastructure/RBAC changes; existing base image and Azure File mount retained. Bicep what-if and Docker rebuild do not apply to this source-only release.
- [x] Back up and create the persistent manual article record.
- [x] Deploy and verify ready revision plus health.
- [x] Verify live Saved and article rendering. Exercise three feed replacements with an isolated copy of the cache and the real persistent manual record; verify production cache bytes remain unchanged.

## 7. Validation Proof

Six tests passed, including repeated syncs, an empty Saved feed, deduplication, body retrieval independent of sync-cache, and authenticated fixture handler rendering. Existing deployed source archive matches repository HEAD exactly. Exact subscription and East US app confirmed through Azure CLI. Infrastructure policy and role validation are unchanged because this release only updates application source and creates a data file; no Bicep/ARM provisioning or Docker image build is involved.

Validation timestamp: 2026-10-29T02:39:39.137498+00:00

`python -m unittest discover -s tests -v`: 6 passed. `python -m py_compile app/main.py tests/test_reader.py`: passed. `git diff --check`: passed. Source tar contains exactly requirements.txt, app/__init__.py, app/main.py, app/static/reader.css, app/static/reader.js; packaged main.py byte match passed. CLI authenticated as existing Azure for Students subscription. Existing public MCR image, Azure File mount, ingress, and role assignments untouched. No pending validation errors.

## Deployment result

Deployed and ready: substack-kindle--0000012. Live authenticated server-side HTTP checks: /saved 200, /article/external-felix-hunger-beats-tenure?from=saved 200. First of 202 Saved entries, date 2026-10-28, no duplicate. Full body, three headings, one image, eight links, and reader controls verified. Three isolated feed replacements passed with the actual manual record. Production feed cache was unchanged. Remote main.py SHA256 matches local: 7c1e97d25d6a7127fc9c647cb8f202b0776cb44b6bcb464b36165b841f864c56.

Source archive: source/manual-saved-20261029T023920Z.tgz. Previous revision 0000011 and previous sourceurl secret retained for rollback. Added read-only source URL secret sourceurl-manual-saved; password, session signing secret, sync token, ingress, base image, and storage mounts unchanged.
