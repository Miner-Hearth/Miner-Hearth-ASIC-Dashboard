# Licence and image record

- Upstream: Miner Hearth 1.17.0 — MIT
  - Source: https://github.com/<github-user>/<repo>/tree/v1.17.0
  - Licence: https://github.com/<github-user>/<repo>/blob/v1.17.0/LICENSE
  - Notice: the MIT notice in LICENSE ("Copyright (c) 2026 Miner Hearth contributors")
- Container: official `ghcr.io/<github-user>/miner-hearth:1.17.0`, built from the upstream
  repository's Dockerfile by its `.github/workflows/docker.yml` (base image `python:3.12-slim`,
  Python Software Foundation licence; Debian packages under their own licences)
  - Multi-architecture index digest: `sha256:<image-digest>`
  - Image documentation: https://github.com/<github-user>/<repo>#linux-docker-and-home-servers
- `icon.png`: the project's own icon (512×512), drawn by `packaging/make_icon.py` in the
  upstream repository, distributed under the project's MIT licence by its authors.
