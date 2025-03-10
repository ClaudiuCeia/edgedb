#
# This source file is part of the EdgeDB open source project.
#
# Copyright 2022-present MagicStack Inc. and the EdgeDB authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#


import datetime
import http
import http.cookies
import json
import logging
import urllib.parse
import base64
import hashlib
import os
import mimetypes
import uuid

from typing import *

import aiosmtplib
from jwcrypto import jwk, jwt

from edb import errors as edb_errors
from edb.common import debug
from edb.common import markup
from edb.ir import statypes
from edb.server import tenant as edbtenant
from edb.server.config.types import CompositeConfigType

from . import oauth, local, errors, util, pkce, ui, config, email as auth_emails


logger = logging.getLogger('edb.server')


class Router:
    test_url: Optional[str]

    def __init__(self, *, db: Any, base_path: str, tenant: edbtenant.Tenant):
        self.db = db
        self.base_path = base_path
        self.tenant = tenant
        self.test_mode = tenant.server.in_test_mode()

    async def handle_request(
        self, request: Any, response: Any, args: list[str]
    ):
        if self.db.db_config is None:
            await self.db.introspection()

        self.test_url = (
            request.params[b'oauth-test-server'].decode()
            if (
                self.test_mode
                and request.params
                and b'oauth-test-server' in request.params
            )
            else None
        )

        handler_args = (request, response)
        try:
            match args:
                # API routes
                case ("authorize",):
                    return await self.handle_authorize(*handler_args)
                case ("callback",):
                    return await self.handle_callback(*handler_args)
                case ("token",):
                    return await self.handle_token(*handler_args)
                case ("register",):
                    return await self.handle_register(*handler_args)
                case ("authenticate",):
                    return await self.handle_authenticate(*handler_args)
                case ("verify",):
                    return await self.handle_verify(*handler_args)
                case ("resend-verification-email",):
                    return await self.handle_resend_verification_email(
                        *handler_args
                    )
                case ('send-reset-email',):
                    return await self.handle_send_reset_email(*handler_args)
                case ('reset-password',):
                    return await self.handle_reset_password(*handler_args)

                # UI routes
                case ('ui', 'signin'):
                    return await self.handle_ui_signin(*handler_args)
                case ('ui', 'signup'):
                    return await self.handle_ui_signup(*handler_args)
                case ('ui', 'forgot-password'):
                    return await self.handle_ui_forgot_password(*handler_args)
                case ('ui', 'reset-password'):
                    return await self.handle_ui_reset_password(*handler_args)
                case ("ui", "verify"):
                    return await self.handle_ui_verify(*handler_args)
                case ("ui", "resend-verification"):
                    return await self.handle_ui_resend_verification(
                        *handler_args
                    )
                case ('ui', '_static', filename):
                    filepath = os.path.join(
                        os.path.dirname(__file__), '_static', filename
                    )
                    try:
                        with open(filepath, 'rb') as f:
                            response.status = http.HTTPStatus.OK
                            response.content_type = (
                                mimetypes.guess_type(filename)[0]
                                or 'application/octet-stream'
                            ).encode()
                            response.body = f.read()
                    except FileNotFoundError:
                        response.status = http.HTTPStatus.NOT_FOUND

                case _:
                    raise errors.NotFound("Unknown auth endpoint")

        except errors.NotFound as ex:
            _fail_with_error(
                response=response,
                status=http.HTTPStatus.NOT_FOUND,
                message=str(ex),
                ex_type=edb_errors.ProtocolError,
            )

        except errors.InvalidData as ex:
            markup.dump(ex)
            _fail_with_error(
                response=response,
                status=http.HTTPStatus.BAD_REQUEST,
                message=str(ex),
                ex_type=edb_errors.ProtocolError,
            )

        except errors.MissingConfiguration as ex:
            _fail_with_error(
                response=response,
                status=http.HTTPStatus.INTERNAL_SERVER_ERROR,
                message=str(ex),
                ex_type=edb_errors.ProtocolError,
            )

        except errors.NoIdentityFound:
            _fail_with_error(
                response=response,
                status=http.HTTPStatus.FORBIDDEN,
                message="No identity found",
                ex_type=edb_errors.ProtocolError,
            )

        except errors.UserAlreadyRegistered as ex:
            _fail_with_error(
                response=response,
                status=http.HTTPStatus.CONFLICT,
                message=str(ex),
                ex_type=edb_errors.ProtocolError,
            )

        except errors.VerificationRequired as ex:
            _fail_with_error(
                response=response,
                status=http.HTTPStatus.UNAUTHORIZED,
                message=str(ex),
                ex_type=edb_errors.ProtocolError,
            )

        except Exception as ex:
            if debug.flags.server:
                markup.dump(ex)
            _fail_with_error(
                response=response,
                status=http.HTTPStatus.INTERNAL_SERVER_ERROR,
                message=str(ex),
                ex_type=edb_errors.InternalServerError,
            )

    async def handle_authorize(self, request: Any, response: Any):
        query = urllib.parse.parse_qs(
            request.url.query.decode("ascii") if request.url.query else ""
        )
        provider_name = _get_search_param(query, "provider")
        redirect_to = _get_search_param(query, "redirect_to")
        redirect_to_on_signup = _maybe_get_search_param(
            query, "redirect_to_on_signup"
        )
        challenge = _get_search_param(query, "challenge")
        oauth_client = oauth.Client(
            db=self.db, provider_name=provider_name, base_url=self.test_url
        )
        await pkce.create(self.db, challenge)
        authorize_url = await oauth_client.get_authorize_url(
            redirect_uri=self._get_callback_url(),
            state=self._make_state_claims(
                provider_name, redirect_to, redirect_to_on_signup, challenge
            ),
        )
        response.status = http.HTTPStatus.FOUND
        response.custom_headers["Location"] = authorize_url

    async def handle_callback(self, request: Any, response: Any):
        if request.method == b"POST" and (
            request.content_type == b"application/x-www-form-urlencoded"
        ):
            form_data = urllib.parse.parse_qs(request.body.decode())
            state = _maybe_get_form_field(form_data, "state")
            code = _maybe_get_form_field(form_data, "code")

            error = _maybe_get_form_field(form_data, "error")
            error_description = _maybe_get_form_field(
                form_data, "error_description"
            )
        elif request.url.query is not None:
            query = urllib.parse.parse_qs(
                request.url.query.decode("ascii") if request.url.query else ""
            )
            state = _maybe_get_search_param(query, "state")
            code = _maybe_get_search_param(query, "code")
            error = _maybe_get_search_param(query, "error")
            error_description = _maybe_get_search_param(
                query, "error_description"
            )
        else:
            raise errors.OAuthProviderFailure(
                "Provider did not respond with expected data"
            )

        if state is None:
            raise errors.InvalidData(
                "Provider did not include the 'state' parameter in " "callback"
            )

        if error is not None:
            try:
                claims = self._verify_and_extract_claims(state)
                redirect_to = cast(str, claims["redirect_to"])
            except Exception:
                raise errors.InvalidData("Invalid state token")

            params = {
                "error": error,
            }
            if error_description is not None:
                params["error_description"] = error_description
            response.custom_headers[
                "Location"
            ] = f"{redirect_to}?{urllib.parse.urlencode(params)}"
            response.status = http.HTTPStatus.FOUND
            return

        if code is None:
            raise errors.InvalidData(
                "Provider did not include the 'code' parameter in " "callback"
            )

        try:
            claims = self._verify_and_extract_claims(state)
            provider_name = cast(str, claims["provider"])
            redirect_to = cast(str, claims["redirect_to"])
            redirect_to_on_signup = cast(
                Optional[str], claims.get("redirect_to_on_signup")
            )
            challenge = cast(str, claims["challenge"])
        except Exception:
            raise errors.InvalidData("Invalid state token")
        oauth_client = oauth.Client(
            db=self.db,
            provider_name=provider_name,
            base_url=self.test_url,
        )
        (
            identity,
            new_identity,
            auth_token,
            refresh_token,
        ) = await oauth_client.handle_callback(code, self._get_callback_url())
        pkce_code = await pkce.link_identity_challenge(
            self.db, identity.id, challenge
        )
        if auth_token or refresh_token:
            await pkce.add_provider_tokens(
                self.db,
                id=pkce_code,
                auth_token=auth_token,
                refresh_token=refresh_token,
            )
        parsed_url = urllib.parse.urlparse(
            (redirect_to_on_signup or redirect_to)
            if new_identity
            else redirect_to
        )
        query_params = urllib.parse.parse_qs(parsed_url.query)
        query_params["code"] = [pkce_code]
        new_query = urllib.parse.urlencode(query_params, doseq=True)
        new_url = parsed_url._replace(query=new_query).geturl()

        session_token = self._make_session_token(identity.id)
        response.status = http.HTTPStatus.FOUND
        response.custom_headers["Location"] = new_url
        _set_cookie(response, "edgedb-session", session_token)

    async def handle_token(self, request: Any, response: Any):
        query = urllib.parse.parse_qs(
            request.url.query.decode("ascii") if request.url.query else ""
        )
        code = _get_search_param(query, "code")
        verifier = _get_search_param(query, "verifier")

        verifier_size = len(verifier)

        if verifier_size < 43:
            raise errors.InvalidData(
                "Verifier must be at least 43 characters long"
            )
        if verifier_size > 128:
            raise errors.InvalidData(
                "Verifier must be shorter than 128 " "characters long"
            )
        try:
            pkce_object = await pkce.get_by_id(self.db, code)
        except Exception:
            raise errors.NoIdentityFound("Could not find a matching PKCE code")

        if pkce_object.identity_id is None:
            raise errors.InvalidData("Code is not associated with an Identity")

        hashed_verifier = hashlib.sha256(verifier.encode()).digest()
        base64_url_encoded_verifier = base64.urlsafe_b64encode(
            hashed_verifier
        ).rstrip(b'=')

        if base64_url_encoded_verifier.decode() == pkce_object.challenge:
            await pkce.delete(self.db, code)
            session_token = self._make_session_token(pkce_object.identity_id)
            response.status = http.HTTPStatus.OK
            response.content_type = b"application/json"
            response.body = json.dumps(
                {
                    "auth_token": session_token,
                    "identity_id": pkce_object.identity_id,
                    "provider_token": pkce_object.auth_token,
                    "provider_refresh_token": (pkce_object.refresh_token),
                }
            ).encode()
        else:
            response.status = http.HTTPStatus.FORBIDDEN

    async def handle_register(self, request: Any, response: Any):
        data = self._get_data_from_request(request)

        maybe_redirect_to = data.get("redirect_to")
        maybe_challenge = data.get("challenge")
        register_provider_name = data.get("provider")
        if register_provider_name is None:
            raise errors.InvalidData('Missing "provider" in register request')

        local_client = local.Client(
            db=self.db, provider_name=register_provider_name
        )
        require_verification = local_client.provider.config.require_verification
        pkce_code: Optional[str] = None

        try:
            identity = await local_client.register(data)
            if not require_verification:
                if maybe_challenge is None:
                    raise errors.InvalidData(
                        'Missing "challenge" in register request'
                    )
                await pkce.create(self.db, maybe_challenge)
                pkce_code = await pkce.link_identity_challenge(
                    self.db, identity.id, maybe_challenge
                )

            await self._send_verification_email(
                provider=register_provider_name,
                identity_id=identity.id,
                to_addr=data["email"],
                verify_url=data.get(
                    "verify_url", f"{self.base_path}/ui/verify"
                ),
                maybe_challenge=maybe_challenge,
                maybe_redirect_to=maybe_redirect_to,
            )

            now_iso8601 = datetime.datetime.now(
                datetime.timezone.utc
            ).isoformat()
            if maybe_redirect_to is not None:
                response.status = http.HTTPStatus.FOUND
                redirect_params = urllib.parse.urlencode(
                    {"verification_email_sent_at": now_iso8601}
                    if require_verification
                    else {"code": pkce_code}
                )
                redirect_url = f"{maybe_redirect_to}?{redirect_params}"
                response.custom_headers["Location"] = redirect_url
            else:
                response.status = http.HTTPStatus.CREATED
                response.content_type = b"application/json"
                if require_verification:
                    response.body = json.dumps(
                        {"verification_email_sent_at": (now_iso8601)}
                    ).encode()
                else:
                    if pkce_code is None:
                        raise errors.PKCECreationFailed
                    response.body = json.dumps({"code": pkce_code}).encode()
        except Exception as ex:
            redirect_on_failure = data.get(
                "redirect_on_failure", maybe_redirect_to
            )
            if redirect_on_failure is not None:
                response.status = http.HTTPStatus.FOUND
                redirect_params = urllib.parse.urlencode(
                    {"error": str(ex), "email": data.get('email', '')}
                )
                redirect_url = f"{redirect_on_failure}?{redirect_params}"
                response.custom_headers["Location"] = redirect_url
            else:
                raise ex

    async def handle_authenticate(self, request: Any, response: Any):
        data = self._get_data_from_request(request)

        authenticate_provider_name = data.get("provider")
        if authenticate_provider_name is None:
            raise errors.InvalidData('Missing "provider" in register request')
        maybe_challenge = data.get("challenge")
        if maybe_challenge is None:
            raise errors.InvalidData('Missing "challenge" in register request')
        await pkce.create(self.db, maybe_challenge)

        local_client = local.Client(
            db=self.db, provider_name=authenticate_provider_name
        )
        try:
            local_identity = await local_client.authenticate(data)
            verified_at = await local_client.get_verified_by_identity_id(
                identity_id=local_identity.id
            )
            if (
                local_client.provider.config.require_verification
                and verified_at is None
            ):
                raise errors.VerificationRequired()

            pkce_code = await pkce.link_identity_challenge(
                self.db, local_identity.id, maybe_challenge
            )
            session_token = self._make_session_token(local_identity.id)
            _set_cookie(response, "edgedb-session", session_token)
            if data.get("redirect_to") is not None:
                response.status = http.HTTPStatus.FOUND
                redirect_params = urllib.parse.urlencode(
                    {
                        "code": pkce_code,
                    }
                )
                redirect_url = f"{data['redirect_to']}?{redirect_params}"
                response.custom_headers["Location"] = redirect_url
            else:
                response.status = http.HTTPStatus.OK
                response.content_type = b"application/json"
                response.body = json.dumps(
                    {
                        "code": pkce_code,
                    }
                ).encode()
        except Exception as ex:
            redirect_on_failure = data.get(
                "redirect_on_failure", data.get("redirect_to")
            )
            if redirect_on_failure is not None:
                response.status = http.HTTPStatus.FOUND
                redirect_params = urllib.parse.urlencode(
                    {
                        "error": str(ex),
                        "email": data.get('email', ''),
                    }
                )
                redirect_url = f"{redirect_on_failure}?{redirect_params}"
                response.custom_headers["Location"] = redirect_url
            else:
                raise ex

    async def handle_verify(self, request: Any, response: Any):
        data = self._get_data_from_request(request)

        _check_keyset(data, {"verification_token", "provider"})

        (
            identity_id,
            issued_at,
            maybe_challenge,
            maybe_redirect_to,
        ) = self._get_data_from_verification_token(data["verification_token"])

        try:
            await self._try_verify_email(
                provider=data["provider"],
                issued_at=issued_at,
                identity_id=identity_id,
            )
        except errors.VerificationTokenExpired:
            response.status = http.HTTPStatus.FORBIDDEN
            response.content_type = b"application/json"
            response.body = json.dumps(
                {
                    "message": (
                        "The 'iat' claim in verification token is older"
                        " than 24 hours"
                    )
                }
            ).encode()
            return

        match (maybe_challenge, maybe_redirect_to):
            case (str(challenge), str(redirect_to)):
                await pkce.create(self.db, challenge)
                code = await pkce.link_identity_challenge(
                    self.db, identity_id, challenge
                )
                response.status = http.HTTPStatus.FOUND
                response.custom_headers["Location"] = _with_appended_qs(
                    redirect_to, {"code": [code]}
                )
            case (str(challenge), _):
                await pkce.create(self.db, challenge)
                code = await pkce.link_identity_challenge(
                    self.db, identity_id, challenge
                )
                response.status = http.HTTPStatus.OK
                response.content_type = b"application/json"
                response.body = json.dumps({"code": code}).encode()
            case (_, str(redirect_to)):
                response.status = http.HTTPStatus.FOUND
                response.custom_headers["Location"] = redirect_to
            case (_, _):
                response.status = http.HTTPStatus.NO_CONTENT

    async def handle_resend_verification_email(
        self, request: Any, response: Any
    ):
        data = self._get_data_from_request(request)

        _check_keyset(data, {"verification_token", "provider"})
        (
            identity_id,
            _,
            maybe_challenge,
            maybe_redirect_to,
        ) = self._get_data_from_verification_token(data["verification_token"])

        local_client = local.Client(db=self.db, provider_name=data["provider"])
        email = await local_client.get_email_by_identity_id(identity_id)
        if email is None:
            await auth_emails.send_fake_email(self.tenant)
        else:
            await self._send_verification_email(
                provider=data["provider"],
                identity_id=identity_id,
                to_addr=email,
                verify_url=f"{self.base_path}/verify",
                maybe_challenge=maybe_challenge,
                maybe_redirect_to=maybe_redirect_to,
            )

        response.status = http.HTTPStatus.OK

    async def handle_send_reset_email(self, request: Any, response: Any):
        data = self._get_data_from_request(request)

        _check_keyset(data, {"provider", "reset_url"})
        local_client = local.Client(db=self.db, provider_name=data["provider"])

        try:
            try:
                (
                    identity,
                    secret,
                ) = await local_client.get_identity_and_secret(data)

                new_reset_token = self._make_secret_token(identity.id, secret)

                reset_token_params = urllib.parse.urlencode(
                    {"reset_token": new_reset_token}
                )
                reset_url = f"{data['reset_url']}?{reset_token_params}"

                await auth_emails.send_password_reset_email(
                    db=self.db,
                    tenant=self.tenant,
                    to_addr=data["email"],
                    reset_url=reset_url,
                    test_mode=self.test_mode,
                )
            except errors.NoIdentityFound:
                await auth_emails.send_fake_email(self.tenant)

            return_data = {
                "email_sent": data.get('email'),
            }

            if data.get("redirect_to") is not None:
                response.status = http.HTTPStatus.FOUND
                redirect_params = urllib.parse.urlencode(return_data)
                redirect_url = f"{data['redirect_to']}?{redirect_params}"
                response.custom_headers["Location"] = redirect_url
            else:
                response.status = http.HTTPStatus.OK
                response.content_type = b"application/json"
                response.body = json.dumps(return_data).encode()
        except aiosmtplib.SMTPException as ex:
            if not debug.flags.server:
                logger.warning("Failed to send emails via SMTP", exc_info=True)
            raise edb_errors.InternalServerError(
                "Failed to send the email, please try again later."
            ) from ex

        except Exception as ex:
            redirect_on_failure = data.get(
                "redirect_on_failure", data.get("redirect_to")
            )
            if redirect_on_failure is not None:
                response.status = http.HTTPStatus.FOUND
                redirect_params = urllib.parse.urlencode(
                    {
                        "error": str(ex),
                        "email": data.get('email', ''),
                    }
                )
                redirect_url = f"{redirect_on_failure}?{redirect_params}"
                response.custom_headers["Location"] = redirect_url
            else:
                raise ex

    async def handle_reset_password(self, request: Any, response: Any):
        data = self._get_data_from_request(request)

        _check_keyset(data, {"provider", "reset_token"})
        local_client = local.Client(db=self.db, provider_name=data["provider"])

        try:
            reset_token = data['reset_token']

            identity_id, secret = self._get_data_from_reset_token(reset_token)

            identity = await local_client.update_password(
                identity_id, secret, data
            )

            session_token = self._make_session_token(identity.id)
            _set_cookie(response, "edgedb-session", session_token)
            if data.get("redirect_to") is not None:
                response.status = http.HTTPStatus.FOUND
                redirect_params = urllib.parse.urlencode(
                    {
                        "identity_id": identity.id,
                        "auth_token": session_token,
                    }
                )
                redirect_url = f"{data['redirect_to']}?{redirect_params}"
                response.custom_headers["Location"] = redirect_url
            else:
                response.status = http.HTTPStatus.OK
                response.content_type = b"application/json"
                response.body = json.dumps(
                    {
                        "identity_id": identity.id,
                        "auth_token": session_token,
                    }
                ).encode()
        except Exception as ex:
            redirect_on_failure = data.get(
                "redirect_on_failure", data.get("redirect_to")
            )
            if redirect_on_failure is not None:
                response.status = http.HTTPStatus.FOUND
                redirect_params = urllib.parse.urlencode(
                    {
                        "error": str(ex),
                        "reset_token": data.get('reset_token', ''),
                    }
                )
                redirect_url = f"{redirect_on_failure}?{redirect_params}"
                response.custom_headers["Location"] = redirect_url
            else:
                raise ex

    async def handle_ui_signin(self, request: Any, response: Any):
        ui_config = self._get_ui_config()

        if ui_config is None:
            response.status = http.HTTPStatus.NOT_FOUND
            response.body = b'Auth UI not enabled'
        else:
            providers = util.maybe_get_config(
                self.db,
                "ext::auth::AuthConfig::providers",
                frozenset,
            )
            if providers is None or len(providers) == 0:
                raise errors.MissingConfiguration(
                    'ext::auth::AuthConfig::providers',
                    'No providers are configured',
                )

            query = urllib.parse.parse_qs(
                request.url.query.decode("ascii") if request.url.query else ""
            )

            maybe_challenge = _get_pkce_challenge(
                response=response,
                cookies=request.cookies,
                query_dict=query,
            )
            if maybe_challenge is None:
                raise errors.InvalidData(
                    'Missing "challenge" in register request'
                )

            response.status = http.HTTPStatus.OK
            response.content_type = b'text/html'
            response.body = ui.render_login_page(
                base_path=self.base_path,
                providers=providers,
                redirect_to=ui_config.redirect_to,
                error_message=_maybe_get_search_param(query, 'error'),
                email=_maybe_get_search_param(query, 'email'),
                challenge=maybe_challenge,
                app_name=ui_config.app_name,
                logo_url=ui_config.logo_url,
                dark_logo_url=ui_config.dark_logo_url,
                brand_color=ui_config.brand_color,
            )

    async def handle_ui_signup(self, request: Any, response: Any):
        ui_config = self._get_ui_config()
        if ui_config is None:
            response.status = http.HTTPStatus.NOT_FOUND
            response.body = b'Auth UI not enabled'
        else:
            providers = util.maybe_get_config(
                self.db,
                "ext::auth::AuthConfig::providers",
                frozenset,
            )
            if providers is None or len(providers) == 0:
                raise errors.MissingConfiguration(
                    'ext::auth::AuthConfig::providers',
                    'No providers are configured',
                )

            query = urllib.parse.parse_qs(
                request.url.query.decode("ascii") if request.url.query else ""
            )

            maybe_challenge = _get_pkce_challenge(
                response=response,
                cookies=request.cookies,
                query_dict=query,
            )
            if maybe_challenge is None:
                raise errors.InvalidData(
                    'Missing "challenge" in register request'
                )

            response.status = http.HTTPStatus.OK
            response.content_type = b'text/html'
            response.body = ui.render_signup_page(
                base_path=self.base_path,
                providers=providers,
                redirect_to=(
                    ui_config.redirect_to_on_signup or ui_config.redirect_to
                ),
                error_message=_maybe_get_search_param(query, 'error'),
                email=_maybe_get_search_param(query, 'email'),
                challenge=maybe_challenge,
                app_name=ui_config.app_name,
                logo_url=ui_config.logo_url,
                dark_logo_url=ui_config.dark_logo_url,
                brand_color=ui_config.brand_color,
            )

    async def handle_ui_forgot_password(self, request: Any, response: Any):
        ui_config = self._get_ui_config()
        password_provider = (
            self._get_password_provider() if ui_config is not None else None
        )

        if ui_config is None or password_provider is None:
            response.status = http.HTTPStatus.NOT_FOUND
            response.body = (
                b'Password provider not configured'
                if ui_config
                else b'Auth UI not enabled'
            )
        else:
            query = urllib.parse.parse_qs(
                request.url.query.decode("ascii") if request.url.query else ""
            )

            response.status = http.HTTPStatus.OK
            response.content_type = b'text/html'
            response.body = ui.render_forgot_password_page(
                base_path=self.base_path,
                provider_name=password_provider.name,
                error_message=_maybe_get_search_param(query, 'error'),
                email=_maybe_get_search_param(query, 'email'),
                email_sent=_maybe_get_search_param(query, 'email_sent'),
                app_name=ui_config.app_name,
                logo_url=ui_config.logo_url,
                dark_logo_url=ui_config.dark_logo_url,
                brand_color=ui_config.brand_color,
            )

    async def handle_ui_reset_password(self, request: Any, response: Any):
        ui_config = self._get_ui_config()
        password_provider = (
            self._get_password_provider() if ui_config is not None else None
        )

        if ui_config is None or password_provider is None:
            response.status = http.HTTPStatus.NOT_FOUND
            response.body = (
                b'Password provider not configured'
                if ui_config
                else b'Auth UI not enabled'
            )
        else:
            query = urllib.parse.parse_qs(
                request.url.query.decode("ascii") if request.url.query else ""
            )

            reset_token = _maybe_get_search_param(query, 'reset_token')

            if reset_token is not None:
                try:
                    (
                        identity_id,
                        secret,
                    ) = self._get_data_from_reset_token(reset_token)

                    local_client = local.Client(
                        db=self.db,
                        provider_name=password_provider.name,
                    )

                    is_valid = await local_client.validate_reset_secret(
                        identity_id, secret
                    )
                except Exception:
                    is_valid = False
            else:
                is_valid = False

            response.status = http.HTTPStatus.OK
            response.content_type = b'text/html'
            response.body = ui.render_reset_password_page(
                base_path=self.base_path,
                provider_name=password_provider.name,
                is_valid=is_valid,
                redirect_to=ui_config.redirect_to,
                reset_token=reset_token,
                error_message=_maybe_get_search_param(query, 'error'),
                app_name=ui_config.app_name,
                logo_url=ui_config.logo_url,
                dark_logo_url=ui_config.dark_logo_url,
                brand_color=ui_config.brand_color,
            )

    async def handle_ui_verify(self, request: Any, response: Any):
        error_messages: list[str] = []
        ui_config = self._get_ui_config()
        if ui_config is None:
            response.status = http.HTTPStatus.NOT_FOUND
            response.body = b'Auth UI not enabled'
            return

        password_provider = (
            self._get_password_provider() if ui_config is not None else None
        )
        is_valid = True
        maybe_pkce_code: str | None = None
        redirect_to = ui_config.redirect_to_on_signup or ui_config.redirect_to

        if password_provider is None:
            response.status = http.HTTPStatus.NOT_FOUND
            response.body = b'Password provider not configured'
            return

        query = urllib.parse.parse_qs(
            request.url.query.decode("ascii") if request.url.query else ""
        )
        maybe_verification_token = _maybe_get_search_param(
            query, "verification_token"
        )
        if maybe_verification_token is None:
            error_messages.append("Missing email verification token.")
            is_valid = False
        else:
            try:
                (
                    identity_id,
                    issued_at,
                    maybe_challenge,
                    maybe_redirect_to,
                ) = self._get_data_from_verification_token(
                    maybe_verification_token
                )
                try:
                    await self._try_verify_email(
                        provider=password_provider.name,
                        issued_at=issued_at,
                        identity_id=identity_id,
                    )
                except errors.VerificationTokenExpired:
                    response.status = http.HTTPStatus.OK
                    response.content_type = b"text/html"
                    response.body = ui.render_email_verification_expired_page(
                        verification_token=maybe_verification_token,
                        app_name=ui_config.app_name,
                        logo_url=ui_config.logo_url,
                        dark_logo_url=ui_config.dark_logo_url,
                        brand_color=ui_config.brand_color,
                    )
                    return

                match maybe_challenge:
                    case str(ch):
                        await pkce.create(self.db, ch)
                        maybe_pkce_code = await pkce.link_identity_challenge(
                            self.db,
                            identity_id,
                            ch,
                        )
                    case _:
                        maybe_pkce_code = None

                match maybe_redirect_to:
                    case str(rt):
                        redirect_to = (
                            _with_appended_qs(
                                rt,
                                {
                                    "code": [maybe_pkce_code],
                                },
                            )
                            if maybe_pkce_code
                            else rt
                        )
                    case _:
                        redirect_to = cast(str, ui_config.redirect_to)

            except Exception as ex:
                error_messages.append(repr(ex))
                is_valid = False

        # Only redirect back if verification succeeds
        if is_valid:
            response.status = http.HTTPStatus.FOUND
            response.custom_headers["Location"] = redirect_to
            return

        response.status = http.HTTPStatus.OK
        response.content_type = b'text/html'
        response.body = ui.render_email_verification_page(
            verification_token=maybe_verification_token,
            is_valid=is_valid,
            error_messages=error_messages,
            app_name=ui_config.app_name,
            logo_url=ui_config.logo_url,
            dark_logo_url=ui_config.dark_logo_url,
            brand_color=ui_config.brand_color,
        )

    async def handle_ui_resend_verification(self, request: Any, response: Any):
        query = urllib.parse.parse_qs(
            request.url.query.decode("ascii") if request.url.query else ""
        )
        ui_config = self._get_ui_config()
        password_provider = (
            self._get_password_provider() if ui_config is not None else None
        )
        is_valid = True

        if password_provider is None:
            response.status = http.HTTPStatus.NOT_FOUND
            response.body = b'Password provider not configured'
            return
        try:
            _check_keyset(query, {"verification_token"})
            verification_token = query["verification_token"][0]
            (
                identity_id,
                _,
                maybe_challenge,
                maybe_redirect_to,
            ) = self._get_data_from_verification_token(verification_token)
            local_client = local.Client(self.db, password_provider.name)
            email = await local_client.get_email_by_identity_id(
                identity_id=identity_id
            )
            await self._send_verification_email(
                provider=password_provider.name,
                identity_id=identity_id,
                to_addr=email,
                verify_url=f"{self.base_path}/ui/verify",
                maybe_challenge=maybe_challenge,
                maybe_redirect_to=maybe_redirect_to,
            )
        except Exception:
            is_valid = False

        response.status = http.HTTPStatus.OK
        response.content_type = b"text/html"
        response.body = ui.render_resend_verification_done_page(
            is_valid=is_valid,
            verification_token=_maybe_get_search_param(
                query, "verification_token"
            ),
            app_name=ui_config.app_name,
            logo_url=ui_config.logo_url,
            dark_logo_url=ui_config.dark_logo_url,
            brand_color=ui_config.brand_color,
        )

    def _get_callback_url(self) -> str:
        return f"{self.base_path}/callback"

    def _get_auth_signing_key(self) -> jwk.JWK:
        auth_signing_key = util.get_config(
            self.db, "ext::auth::AuthConfig::auth_signing_key"
        )
        key_bytes = base64.b64encode(auth_signing_key.encode())

        return jwk.JWK(kty="oct", k=key_bytes.decode())

    def _make_state_claims(
        self,
        provider: str,
        redirect_to: str,
        redirect_to_on_signup: Optional[str],
        challenge: str,
    ) -> str:
        signing_key = self._get_auth_signing_key()
        expires_at = datetime.datetime.now(
            datetime.timezone.utc
        ) + datetime.timedelta(minutes=5)

        state_claims = {
            "iss": self.base_path,
            "provider": provider,
            "exp": expires_at.timestamp(),
            "redirect_to": redirect_to,
            "challenge": challenge,
        }
        if redirect_to_on_signup:
            state_claims['redirect_to_on_signup'] = redirect_to_on_signup
        state_token = jwt.JWT(
            header={"alg": "HS256"},
            claims=state_claims,
        )
        state_token.make_signed_token(signing_key)
        return state_token.serialize()

    def _make_session_token(self, identity_id: str) -> str:
        signing_key = self._get_auth_signing_key()
        auth_expiration_time = util.get_config(
            self.db,
            "ext::auth::AuthConfig::token_time_to_live",
            statypes.Duration,
        )
        expires_in = auth_expiration_time.to_timedelta()
        expires_at = datetime.datetime.now(datetime.timezone.utc) + expires_in

        claims: dict[str, Any] = {
            "iss": self.base_path,
            "sub": identity_id,
        }
        if expires_in.total_seconds() != 0:
            claims["exp"] = expires_at.timestamp()
        session_token = jwt.JWT(
            header={"alg": "HS256"},
            claims=claims,
        )
        session_token.make_signed_token(signing_key)
        return session_token.serialize()

    def _get_from_claims(self, state: str, key: str) -> str:
        signing_key = self._get_auth_signing_key()
        try:
            state_token = jwt.JWT(key=signing_key, jwt=state)
        except Exception:
            raise errors.InvalidData("Invalid state token")
        state_claims: dict[str, str] = json.loads(state_token.claims)
        value = state_claims.get(key)
        if value is None:
            raise errors.InvalidData("Invalid state token")
        return value

    def _make_secret_token(
        self,
        identity_id: str,
        secret: str,
        additional_claims: dict[str, str | int | float | bool | None]
        | None = None,
        expires_in: datetime.timedelta | None = None,
    ) -> str:
        signing_key = self._get_auth_signing_key()
        expires_in = (
            datetime.timedelta(minutes=10) if expires_in is None else expires_in
        )
        expires_at = datetime.datetime.now(datetime.timezone.utc) + expires_in

        claims: dict[str, Any] = {
            "iss": self.base_path,
            "sub": identity_id,
            "jti": secret,
            **(additional_claims or {}),
        }
        if expires_in.total_seconds() != 0:
            claims["exp"] = expires_at.timestamp()
        session_token = jwt.JWT(
            header={"alg": "HS256"},
            claims=claims,
        )
        session_token.make_signed_token(signing_key)
        return session_token.serialize()

    def _verify_and_extract_claims(
        self, jwtStr: str
    ) -> dict[str, str | int | float | bool]:
        signing_key = self._get_auth_signing_key()
        verified = jwt.JWT(key=signing_key, jwt=jwtStr)
        return json.loads(verified.claims)

    def _get_data_from_reset_token(self, token: str) -> Tuple[str, str]:
        try:
            claims = self._verify_and_extract_claims(token)
        except Exception:
            raise errors.InvalidData("Invalid 'reset_token'")

        identity_id = cast(Optional[str], claims.get('sub'))
        secret = cast(Optional[str], claims.get('jti'))

        if identity_id is None or secret is None:
            raise errors.InvalidData("Invalid 'reset_token'")

        return (identity_id, secret)

    def _get_data_from_verification_token(
        self, token: str
    ) -> Tuple[str, float, Optional[str], Optional[str]]:
        try:
            claims = self._verify_and_extract_claims(token)
        except Exception:
            raise errors.InvalidData("Invalid 'verification_token'")

        identity_id = claims["sub"]
        maybe_challenge = claims.get("challenge")
        if maybe_challenge is not None and not isinstance(maybe_challenge, str):
            raise errors.InvalidData(
                "Invalid 'challenge' in 'verification_token'"
            )

        maybe_redirect_to = claims.get("redirect_to")
        if maybe_redirect_to is not None and not isinstance(
            maybe_redirect_to, str
        ):
            raise errors.InvalidData(
                "Invalid 'redirect_to' in 'verification_token'"
            )

        maybe_issued_at = claims.get("iat")
        if maybe_issued_at is None:
            raise errors.InvalidData("Missing 'iat' in 'verification_token'")

        return_value: Tuple[str, float, Optional[str], Optional[str]]
        match (
            identity_id,
            maybe_issued_at,
            maybe_challenge,
            maybe_redirect_to,
        ):
            case (str(id), float(issued_at), challenge, redirect_to):
                return_value = (id, issued_at, challenge, redirect_to)
            case (_, _, _, _):
                raise errors.InvalidData(
                    "Invalid claims in 'verification_token'"
                )
        return return_value

    def _get_data_from_request(self, request: Any) -> dict[Any, Any]:
        content_type = request.content_type
        match content_type:
            case b"application/x-www-form-urlencoded":
                return {
                    k: v[0]
                    for k, v in urllib.parse.parse_qs(
                        request.body.decode('ascii')
                    ).items()
                }
            case b"application/json":
                data = json.loads(request.body)
                if not isinstance(data, dict):
                    raise errors.InvalidData(
                        f"Invalid json data, expected an object"
                    )
                return data
            case _:
                raise errors.InvalidData(
                    f"Unsupported Content-Type: {content_type}"
                )

    def _get_ui_config(self):
        return cast(config.UIConfig, util.maybe_get_config(
            self.db, "ext::auth::AuthConfig::ui",
            CompositeConfigType
        ))

    def _get_password_provider(self):
        providers = cast(list[config.ProviderConfig], util.get_config(
            self.db,
            "ext::auth::AuthConfig::providers",
            frozenset,
        ))
        password_providers = [
            p for p in providers if (p.name == 'builtin::local_emailpassword')
        ]

        return password_providers[0] if len(password_providers) == 1 else None

    async def _send_verification_email(
        self,
        *,
        verify_url: str,
        provider: str,
        identity_id: str,
        to_addr: str,
        maybe_challenge: str | None,
        maybe_redirect_to: str | None,
    ):
        # Generate verification token
        issued_at = datetime.datetime.now(datetime.timezone.utc).timestamp()
        verification_token = self._make_secret_token(
            identity_id=identity_id,
            secret=str(uuid.uuid4()),
            additional_claims={
                "iat": issued_at,
                "challenge": maybe_challenge,
                "redirect_to": maybe_redirect_to,
            },
            expires_in=datetime.timedelta(seconds=0),
        )
        await auth_emails.send_verification_email(
            db=self.db,
            tenant=self.tenant,
            to_addr=to_addr,
            verification_token=verification_token,
            provider=provider,
            verify_url=verify_url,
            test_mode=self.test_mode,
        )

    async def _try_verify_email(
        self, provider: str, issued_at: float, identity_id: str
    ) -> None:
        current_time = datetime.datetime.now(datetime.timezone.utc)
        issued_at_datetime = datetime.datetime.fromtimestamp(
            issued_at, datetime.timezone.utc
        )
        token_age = current_time - issued_at_datetime
        if token_age > datetime.timedelta(hours=24):
            raise errors.VerificationTokenExpired()

        local_client = local.Client(db=self.db, provider_name=provider)
        updated = await local_client.verify_email(identity_id, current_time)
        if updated is None:
            raise errors.NoIdentityFound(
                "Could not verify email for identity"
                f" {identity_id}. This email address may not exist"
                " in our system, or it might already be verified."
            )


def _fail_with_error(
    *,
    response: Any,
    status: http.HTTPStatus,
    message: str,
    ex_type: Any,
):
    err_dct = {
        "message": message,
        "type": str(ex_type.__name__),
        "code": ex_type.get_code(),
    }

    response.body = json.dumps({"error": err_dct}).encode()
    response.status = status


def _maybe_get_search_param(
    query_dict: dict[str, list[str]], key: str
) -> str | None:
    params = query_dict.get(key)
    return params[0] if params else None


def _get_search_param(query_dict: dict[str, list[str]], key: str) -> str:
    val = _maybe_get_search_param(query_dict, key)
    if val is None:
        raise errors.InvalidData(f"Missing query parameter: {key}")
    return val


def _maybe_get_form_field(
    form_dict: dict[str, list[str]], key: str
) -> str | None:
    maybe_val = form_dict.get(key)
    if maybe_val is None:
        return None
    return maybe_val[0]


def _get_pkce_challenge(
    *,
    response,
    cookies: http.cookies.SimpleCookie,
    query_dict: dict[str, list[str]],
) -> str | None:
    cookie_name = 'edgedb-pkce-challenge'
    challenge: str | None = _maybe_get_search_param(query_dict, 'challenge')
    if challenge is not None:
        _set_cookie(response, cookie_name, challenge)
    else:
        if 'edgedb-pkce-challenge' in cookies:
            challenge = cookies['edgedb-pkce-challenge'].value
    return challenge


def _set_cookie(
    response: Any,
    name: str,
    value: str,
    http_only: bool = True,
    secure: bool = True,
    same_site: str = "Strict",
):
    val: http.cookies.Morsel = http.cookies.SimpleCookie({name: value})[name]
    val["httponly"] = http_only
    val["secure"] = secure
    val["samesite"] = same_site
    response.custom_headers["Set-Cookie"] = val.OutputString()


def _with_appended_qs(url: str, query: dict[str, list[str]]) -> str:
    url_parts = list(urllib.parse.urlparse(url))
    existing_query = urllib.parse.parse_qs(url_parts[4])
    existing_query.update(query)

    url_parts[4] = urllib.parse.urlencode(existing_query, doseq=True)
    return urllib.parse.urlunparse(url_parts)


def _check_keyset(candidate: dict[str, Any], keyset: set[str]):
    missing_fields = [field for field in keyset if field not in candidate]
    if missing_fields:
        raise errors.InvalidData(
            "Missing required fields: " ", ".join(missing_fields)
        )
