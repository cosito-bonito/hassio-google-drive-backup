import re

from yarl import URL
from datetime import timedelta
from backup.logger import getLogger
from backup.config import Setting, Config
from backup.time import Time
from aiohttp.web import (HTTPBadRequest, HTTPNotFound,
                         HTTPUnauthorized, Request, Response, delete, get,
                         json_response, patch, post, put, HTTPSeeOther)
from injector import inject, singleton
from .base_server import BaseServer, bytesPattern, intPattern
from .ports import Ports
from typing import Any, Dict
from asyncio import Event
from backup.creds import Creds

logger = getLogger(__name__)

mimeTypeQueryPattern = re.compile("^mimeType='.*'$")
parentsQueryPattern = re.compile("^'.*' in parents$")
resumeBytesPattern = re.compile("^bytes \\*/\\d+$")

URL_MATCH_UPLOAD = "^/upload/drive/v3/files/$"
URL_MATCH_UPLOAD_PROGRESS = "^/upload/drive/v3/files/progress/.*$"
URL_MATCH_CREATE = "^/upload/drive/v3/files/progress/.*$"
URL_MATCH_FILE = "^/drive/v3/files/.*$"


@singleton
class SimulatedGoogle(BaseServer):
    @inject
    def __init__(self, config: Config, time: Time, ports: Ports):
        self._time = time
        self.config = config

        # auth state
        self._custom_drive_client_id = self.generateId(5)
        self._custom_drive_client_secret = self.generateId(5)
        self._drive_auth_code = "drive_auth_code"
        self._port = ports.server
        self._auth_token = ""
        self._refresh_token = "test_refresh_token"
        self._client_id_hack = None

        # Drive item states
        self.items = {}
        self.lostPermission = []
        self.space_available = 1024 * 1024 * 100  # 100 Mb

        # Upload state information
        self._upload_info: Dict[str, Any] = {}
        self.chunks = []
        self._upload_chunk_wait = Event()
        self._upload_chunk_trigger = Event()
        self._current_chunk = 1
        self._waitOnChunk = 0

    def setDriveSpaceAvailable(self, bytes_available):
        self.space_available = bytes_available

    def generateNewAccessToken(self):
        new_token = self.generateId(20)
        self._auth_token = new_token

    def generateNewRefreshToken(self):
        new_token = self.generateId(20)
        self._refresh_token = new_token

    def expireCreds(self):
        self.generateNewAccessToken()
        self.generateNewRefreshToken()

    def expireRefreshToken(self):
        self.generateNewRefreshToken()

    def resetDriveAuth(self):
        self.expireCreds()
        self.config.override(Setting.DEFAULT_DRIVE_CLIENT_ID, self.generateId(5))
        self.config.override(Setting.DEFAULT_DRIVE_CLIENT_SECRET, self.generateId(5))

    def creds(self):
        return Creds(self._time,
                     id=self.config.get(Setting.DEFAULT_DRIVE_CLIENT_ID),
                     expiration=self._time.now() + timedelta(hours=1),
                     access_token=self._auth_token,
                     refresh_token=self._refresh_token)

    def routes(self):
        return [
            put('/upload/drive/v3/files/progress/{id}', self._uploadProgress),
            post('/upload/drive/v3/files/', self._upload),
            post('/drive/v3/files/', self._create),
            get('/drive/v3/files/', self._query),
            delete('/drive/v3/files/{id}/', self._delete),
            patch('/drive/v3/files/{id}/', self._update),
            get('/drive/v3/files/{id}/', self._get),
            post('/oauth2/v4/token', self._oauth2Token),
            get('/o/oauth2/v2/auth', self._oAuth2Authorize),
            get('/drive/customcreds', self._getCustomCred),
            post('/token', self._driveToken),
        ]

    async def _checkDriveHeaders(self, request: Request):
        if request.headers.get("Authorization", "") != "Bearer " + self._auth_token:
            raise HTTPUnauthorized()

    async def _oAuth2Authorize(self, request: Request):
        query = request.query
        if query.get('client_id') != self.config.get(Setting.DEFAULT_DRIVE_CLIENT_ID) and query.get('client_id') != self._custom_drive_client_id:
            raise HTTPUnauthorized()
        if query.get('scope') != 'https://www.googleapis.com/auth/drive.file':
            raise HTTPUnauthorized()
        if query.get('response_type') != 'code':
            raise HTTPUnauthorized()
        if query.get('include_granted_scopes') != 'true':
            raise HTTPUnauthorized()
        if query.get('access_type') != 'offline':
            raise HTTPUnauthorized()
        if 'state' not in query:
            raise HTTPUnauthorized()
        if 'redirect_uri' not in query:
            raise HTTPUnauthorized()
        if query.get('prompt') != 'consent':
            raise HTTPUnauthorized()
        if query.get('redirect_uri') == 'urn:ietf:wg:oauth:2.0:oob':
            return json_response({"code": self._drive_auth_code})
        url = URL(query.get('redirect_uri')).with_query({'code': self._drive_auth_code, 'state': query.get('state')})
        raise HTTPSeeOther(str(url))

    async def _getCustomCred(self, request: Request):
        return json_response({
            "client_id": self._custom_drive_client_id,
            "client_secret": self._custom_drive_client_secret
        })

    async def _driveToken(self, request: Request):
        data = await request.post()
        if data.get('redirect_uri') not in ["http://localhost:{}/drive/authorize".format(self._port), 'urn:ietf:wg:oauth:2.0:oob']:
            raise HTTPUnauthorized()
        if data.get('grant_type') != 'authorization_code':
            raise HTTPUnauthorized()
        if not self._checkClientIdandSecret(data.get('client_id'), data.get('client_secret')):
            raise HTTPUnauthorized()
        if data.get('code') != self._drive_auth_code:
            raise HTTPUnauthorized()
        self.generateNewRefreshToken()
        return json_response({
            'access_token': self._auth_token,
            'refresh_token': self._refresh_token,
            'client_id': data.get('client_id'),
            'client_secret': self.config.get(Setting.DEFAULT_DRIVE_CLIENT_SECRET),
            'token_expiry': self.timeToRfc3339String(self._time.now()),
        })

    def _checkClientIdandSecret(self, client_id: str, client_secret: str) -> bool:
        if self._custom_drive_client_id == client_id and self._custom_drive_client_secret == client_secret:
            return True
        if client_id == self.config.get(Setting.DEFAULT_DRIVE_CLIENT_ID) == client_id and client_secret == self.config.get(Setting.DEFAULT_DRIVE_CLIENT_SECRET):
            return True

        if self._client_id_hack is not None:
            if client_id == self._client_id_hack and client_secret == self.config.get(Setting.DEFAULT_DRIVE_CLIENT_SECRET):
                return True
        return False

    async def _oauth2Token(self, request: Request):
        params = await request.post()
        if not self._checkClientIdandSecret(params['client_id'], params['client_secret']):
            raise HTTPUnauthorized()
        if params['refresh_token'] != self._refresh_token:
            raise HTTPUnauthorized()
        if params['grant_type'] != 'refresh_token':
            raise HTTPUnauthorized()

        self.generateNewAccessToken()

        return json_response({
            'access_token': self._auth_token,
            'expires_in': 3600,
            'token_type': 'doesn\'t matter'
        })

    def filter_fields(self, item: Dict[str, Any], fields) -> Dict[str, Any]:
        ret = {}
        for field in fields:
            if field in item:
                ret[field] = item[field]
        return ret

    def parseFields(self, source: str):
        fields = []
        for field in source.split(","):
            if field.startswith("files("):
                fields.append(field[6:])
            elif field.endswith(")"):
                fields.append(field[:-1])
            else:
                fields.append(field)
        return fields

    def formatItem(self, base, id):
        base['capabilities'] = {'canAddChildren': True,
                                'canListChildren': True, 'canDeleteChildren': True}
        base['trashed'] = False
        base['id'] = id
        base['modifiedTime'] = self.timeToRfc3339String(self._time.now())
        return base

    async def _get(self, request: Request):
        id = request.match_info.get('id')
        await self._checkDriveHeaders(request)
        if id not in self.items:
            raise HTTPNotFound()
        if id in self.lostPermission:
            return Response(
                status=403,
                content_type="application/json",
                text='{"error": {"errors": [{"reason": "forbidden"}]}}')
        request_type = request.query.get("alt", "metadata")
        if request_type == "media":
            # return bytes
            item = self.items[id]
            if 'bytes' not in item:
                raise HTTPBadRequest()
            return self.serve_bytes(request, item['bytes'], include_length=False)
        else:
            fields = request.query.get("fields", "id").split(",")
            return json_response(self.filter_fields(self.items[id], fields))

    async def _update(self, request: Request):
        id = request.match_info.get('id')
        await self._checkDriveHeaders(request)
        if id not in self.items:
            return HTTPNotFound
        update = await request.json()
        for key in update:
            if key in self.items[id] and isinstance(self.items[id][key], dict):
                self.items[id][key].update(update[key])
            else:
                self.items[id][key] = update[key]
        return Response()

    async def _delete(self, request: Request):
        id = request.match_info.get('id')
        await self._checkDriveHeaders(request)
        if id not in self.items:
            raise HTTPNotFound()
        del self.items[id]
        return Response()

    async def _query(self, request: Request):
        await self._checkDriveHeaders(request)
        query: str = request.query.get("q", "")
        fields = self.parseFields(request.query.get('fields', 'id'))
        if mimeTypeQueryPattern.match(query):
            ret = []
            mimeType = query[len("mimeType='"):-1]
            for item in self.items.values():
                if item.get('mimeType', '') == mimeType:
                    ret.append(self.filter_fields(item, fields))
            return json_response({'files': ret})
        elif parentsQueryPattern.match(query):
            ret = []
            parent = query[1:-len("' in parents")]
            if parent not in self.items:
                raise HTTPNotFound()
            if parent in self.lostPermission:
                return Response(
                    status=403,
                    content_type="application/json",
                    text='{"error": {"errors": [{"reason": "forbidden"}]}}')
            for item in self.items.values():
                if parent in item.get('parents', []):
                    ret.append(self.filter_fields(item, fields))
            return json_response({'files': ret})
        elif len(query) == 0:
            ret = []
            for item in self.items.values():
                ret.append(self.filter_fields(item, fields))
            return json_response({'files': ret})
        else:
            raise HTTPBadRequest

    async def _create(self, request: Request):
        await self._checkDriveHeaders(request)
        id = self.generateId(30)
        item = self.formatItem(await request.json(), id)
        self.items[id] = item
        return json_response({'id': item['id']})

    async def _upload(self, request: Request):
        logger.info("Drive start upload request")
        await self._checkDriveHeaders(request)
        if request.query.get('uploadType') != 'resumable':
            raise HTTPBadRequest()
        mimeType = request.headers.get('X-Upload-Content-Type', None)
        if mimeType is None:
            raise HTTPBadRequest()
        size = int(request.headers.get('X-Upload-Content-Length', -1))
        if size < 0:
            raise HTTPBadRequest()
        total_size = 0
        for item in self.items.values():
            total_size += item.get('size', 0)
        total_size += size
        if total_size > self.space_available:
            return json_response({
                "error": {
                    "errors": [
                        {"reason": "storageQuotaExceeded"}
                    ]
                }
            }, status=400)
        metadata = await request.json()
        id = self.generateId()

        # Validate parents
        if 'parents' in metadata:
            for parent in metadata['parents']:
                if parent not in self.items:
                    raise HTTPNotFound()
                if parent in self.lostPermission:
                    return Response(status=403, content_type="application/json", text='{"error": {"errors": [{"reason": "forbidden"}]}}')
        self._upload_info['size'] = size
        self._upload_info['mime'] = mimeType
        self._upload_info['item'] = self.formatItem(metadata, id)
        self._upload_info['id'] = id
        self._upload_info['next_start'] = 0
        metadata['bytes'] = bytearray()
        metadata['size'] = size
        resp = Response()
        resp.headers['Location'] = "http://localhost:" + \
            str(self._port) + "/upload/drive/v3/files/progress/" + id
        return resp

    async def _uploadProgress(self, request: Request):
        if self._waitOnChunk > 0:
            if self._current_chunk == self._waitOnChunk:
                self._upload_chunk_trigger.set()
                await self._upload_chunk_wait.wait()
            else:
                self._current_chunk += 1
        id = request.match_info.get('id')
        await self._checkDriveHeaders(request)
        if self._upload_info.get('id', "") != id:
            raise HTTPBadRequest()
        chunk_size = int(request.headers['Content-Length'])
        info = request.headers['Content-Range']
        if resumeBytesPattern.match(info):
            resp = Response(status=308)
            if self._upload_info['next_start'] != 0:
                resp.headers['Range'] = "bytes=0-{0}".format(self._upload_info['next_start'] - 1)
            return resp
        if not bytesPattern.match(info):
            raise HTTPBadRequest()
        numbers = intPattern.findall(info)
        start = int(numbers[0])
        end = int(numbers[1])
        total = int(numbers[2])
        if total != self._upload_info['size']:
            raise HTTPBadRequest()
        if start != self._upload_info['next_start']:
            raise HTTPBadRequest()
        if not (end == total - 1 or chunk_size % (256 * 1024) == 0):
            raise HTTPBadRequest()
        if end > total - 1:
            raise HTTPBadRequest()

        # get the chunk
        received_bytes = await self.readAll(request)

        # validate the chunk
        if len(received_bytes) != chunk_size:
            raise HTTPBadRequest()

        if len(received_bytes) != end - start + 1:
            raise HTTPBadRequest()

        self._upload_info['item']['bytes'].extend(received_bytes)

        if len(self._upload_info['item']['bytes']) != end + 1:
            raise HTTPBadRequest()

        self.chunks.append(len(received_bytes))
        if end == total - 1:
            # upload is complete, so create the item
            self.items[self._upload_info['id']] = self._upload_info['item']
            return json_response({"id": self._upload_info['id']})
        else:
            # Return an incomplete response
            # For some reason, the tests like to stop right here
            resp = Response(status=308)
            self._upload_info['next_start'] = end + 1
            resp.headers['Range'] = "bytes=0-{0}".format(end)
            return resp
