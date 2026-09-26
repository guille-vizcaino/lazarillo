# Releasing

Releases go to PyPI from GitHub Actions (`.github/workflows/release.yml`) with
[Trusted Publishing](https://docs.pypi.org/trusted-publishers/), so no API tokens are stored.

## One-time setup

1. On [PyPI](https://pypi.org/manage/account/publishing/) and
   [TestPyPI](https://test.pypi.org/manage/account/publishing/), add a pending publisher:
   project `lazarillo`, owner `guille-vizcaino`, repository `lazarillo`,
   workflow `release.yml`, environment `pypi` (PyPI) or `testpypi` (TestPyPI).
2. In the GitHub repository settings, create the `testpypi` and `pypi` environments.
   Adding yourself as a required reviewer on `pypi` gives a last manual check before the real upload.

## Cutting a release

1. Bump `__version__` in `src/lazarillo/__init__.py` and add the release to `CHANGELOG.md`.
2. Merge to `main` with CI green.
3. Tag and push: `git tag v0.1.0 && git push origin v0.1.0`.

The workflow checks that the tag matches `__version__`, builds the wheel and sdist once,
publishes to TestPyPI and then to PyPI. To try the TestPyPI build:

```bash
pip install -i https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ lazarillo
```
