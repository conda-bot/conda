# Copyright (C) 2012 Anaconda, Inc
# SPDX-License-Identifier: BSD-3-Clause
import json
import warnings
from functools import lru_cache
from glob import glob
from logging import getLogger
from os import makedirs
from os.path import basename, exists, isdir, join

try:
    from conda_content_trust.authentication import verify_delegation, verify_root
    from conda_content_trust.common import (
        SignatureError,
        load_metadata_from_file,
        write_metadata_to_file,
    )
    from conda_content_trust.signing import wrap_as_signable
except ImportError:
    # _SignatureVerification.enabled handles the rest of this state
    class SignatureError(Exception):
        pass


from ..base.context import context
from ..common.url import join_url
from ..gateways.connection import HTTPError, InsecureRequestWarning
from ..gateways.connection.session import CondaSession
from .constants import INITIAL_TRUST_ROOT, KEY_MGR_FILE

log = getLogger(__name__)


class _SignatureVerification:
    # FUTURE: Python 3.8+, replace with functools.cached_property
    @property
    @lru_cache(maxsize=None)
    def enabled(self):
        # safety checks must be enabled
        if not context.extra_safety_checks:
            return False

        # signing url must be defined
        if not context.signing_metadata_url_base:
            log.warn(
                "metadata signature verification requested, "
                "but no metadata URL base has not been specified."
            )
            return False

        # conda_content_trust must be installed
        try:
            import conda_content_trust  # noqa: F401
        except ImportError:
            log.warn(
                "metadata signature verification requested, "
                "but `conda-content-trust` is not installed."
            )
            return False

        # create artifact verification directory if missing
        if not isdir(context.av_data_dir):
            log.info("creating directory for artifact verification metadata")
            makedirs(context.av_data_dir)

        # ensure the trusted_root exists
        if self.trusted_root is None:
            log.warn(
                "could not find trusted_root data for metadata signature verification"
            )
            return False

        # ensure the key_mgr exists
        if self.key_mgr is None:
            log.warn("could not find key_mgr data for metadata signature verification")
            return False

        # signature verification is enabled
        return True

    # FUTURE: Python 3.8+, replace with functools.cached_property
    @property
    @lru_cache(maxsize=None)
    def trusted_root(self):
        # TODO: formalize paths for `*.root.json` and `key_mgr.json` on server-side
        trusted = INITIAL_TRUST_ROOT

        # Load current trust root metadata from filesystem
        for path in sorted(
            glob(join(context.av_data_dir, "[0-9]*.root.json")), reverse=True
        ):
            try:
                int(basename(path).split(".")[0])
            except ValueError:
                # prefix is not an int and is consequently an invalid file, skip to the next
                pass
            else:
                log.info(f"Loading root metadata from {path}.")
                trusted = load_metadata_from_file(path)
                break
        else:
            log.debug(
                f"No root metadata in {context.av_data_dir}. "
                "Using built-in root metadata."
            )

        # Refresh trust root metadata
        more_signatures = True
        while more_signatures:
            # TODO: caching mechanism to reduce number of refresh requests
            fname = f"{trusted['signed']['version'] + 1}.root.json"
            path = join(context.av_data_dir, fname)

            try:
                # TODO: support fetching root data with credentials
                untrusted = self._fetch_channel_signing_data(
                    context.signing_metadata_url_base,
                    fname,
                )

                verify_root(trusted, untrusted)
            except HTTPError as err:
                # HTTP 404 implies no updated root.json is available, which is
                # not really an "error" and does not need to be logged.
                if err.response.status_code != 404:
                    log.error(err)
                more_signatures = False
            except Exception as err:
                # TODO: more error handling
                log.error(err)
                more_signatures = False
            else:
                # New trust root metadata checks out
                trusted = untrusted
                write_metadata_to_file(trusted, path)

        return trusted

    # FUTURE: Python 3.8+, replace with functools.cached_property
    @property
    @lru_cache(maxsize=None)
    def key_mgr(self):
        trusted = None

        # Refresh key manager metadata
        fname = KEY_MGR_FILE
        path = join(context.av_data_dir, fname)

        try:
            untrusted = self._fetch_channel_signing_data(
                context.signing_metadata_url_base,
                KEY_MGR_FILE,
            )

            verify_delegation("key_mgr", untrusted, self.trusted_root)
        except (ConnectionError, HTTPError) as err:
            log.warn(err)
        except Exception as err:
            # TODO: more error handling
            raise
            log.error(err)
        else:
            # New key manager metadata checks out
            trusted = untrusted
            write_metadata_to_file(trusted, path)

        # If key_mgr is unavailable from server, fall back to copy on disk
        if not trusted and exists(path):
            trusted = load_metadata_from_file(path)

        return trusted

    # FUTURE: Python 3.8+, replace with functools.cached_property
    @property
    @lru_cache(maxsize=None)
    def session(self):
        return CondaSession()

    def _fetch_channel_signing_data(
        self, signing_data_url, filename, etag=None, mod_stamp=None
    ):
        if not context.ssl_verify:
            warnings.simplefilter("ignore", InsecureRequestWarning)

        headers = {
            "Accept-Encoding": "gzip, deflate, compress, identity",
            "Content-Type": "application/json",
        }
        if etag:
            headers["If-None-Match"] = etag
        if mod_stamp:
            headers["If-Modified-Since"] = mod_stamp

        try:
            # The `auth` argument below looks a bit weird, but passing `None` seems
            # insufficient for suppressing modifying the URL to add an Anaconda
            # server token; for whatever reason, we must pass an actual callable in
            # order to suppress the HTTP auth behavior configured in the session.
            #
            # TODO: Figure how to handle authn for obtaining trust metadata,
            # independently of the authn used to access package repositories.
            resp = self.session.get(
                join_url(signing_data_url, filename),
                headers=headers,
                proxies=self.session.proxies,
                auth=lambda r: r,
                timeout=(
                    context.remote_connect_timeout_secs,
                    context.remote_read_timeout_secs,
                ),
            )

            resp.raise_for_status()
        except:
            # TODO: more sensible error handling
            raise

        # In certain cases (e.g., using `-c` access anaconda.org channels), the
        # `CondaSession.get()` retry logic combined with the remote server's
        # behavior can result in non-JSON content being returned.  Parse returned
        # content here (rather than directly in the return statement) so callers of
        # this function only have to worry about a ValueError being raised.
        try:
            return resp.json()
        except json.decoder.JSONDecodeError as err:  # noqa
            # TODO: additional loading and error handling improvements?
            raise ValueError(
                f"Invalid JSON returned from {signing_data_url}/{filename}"
            )

    def __call__(self, info, fn, signatures):
        if not self.enabled or fn not in signatures:
            return

        # create a signable envelope (a dict with the info and signatures)
        envelope = wrap_as_signable(info)
        envelope["signatures"] = signatures[fn]

        try:
            verify_delegation("pkg_mgr", envelope, self.key_mgr)
        except SignatureError:
            log.warn(f"invalid signature for {fn}")
            status = "(WARNING: metadata signature verification failed)"
        else:
            status = "(INFO: package metadata is signed by Anaconda and trusted)"

        info["metadata_signature_status"] = status


# singleton for caching
signature_verification = _SignatureVerification()
