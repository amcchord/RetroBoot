"""Typed Raspberry Pi display presets; the ordinary pairing/CSRF middleware applies."""
from aiohttp import web

from ..rpidisplay import DisplayError


def register(server, router):
    api = PiDisplayApi(server)
    router.add_get('/api/v1/pi-display', api.status)
    router.add_post('/api/v1/pi-display', api.select)
    router.add_post('/api/v1/pi-display/confirm', api.confirm)


class PiDisplayApi:
    def __init__(self, server):
        self.server = server
        self.hostd = server.hostd

    @property
    def display(self):
        return self.hostd.rpi_display

    async def status(self, request):
        return web.json_response(self.display.public())

    async def _body(self, request, fields):
        from .server import ApiError
        data = await self.server.body(request)
        unknown = set(data) - set(fields)
        if unknown:
            raise ApiError(422, 'invalid', "Some fields aren't recognized.", {k: 'Unknown field.' for k in unknown})
        return data

    def _error(self, exc):
        from .server import ApiError
        return ApiError(409 if exc.code in ('stale-revision', 'display-unavailable', 'display-trial') else 422,
                        exc.code, str(exc))

    async def select(self, request):
        data = await self._body(request, ('profile', 'revision'))
        try:
            result = self.display.select(data.get('profile'), data.get('revision'))
        except DisplayError as exc:
            raise self._error(exc)
        except OSError:
            from .server import ApiError
            raise ApiError(409, 'display-write-failed', 'The startup card could not save the display. Check that it is writable.')
        return web.json_response(result)

    async def confirm(self, request):
        from .server import ApiError
        data = await self._body(request, ('keep', 'revision'))
        if type(data.get('keep')) is not bool:
            raise ApiError(422, 'invalid', 'keep must be true or false.', {'keep': 'Must be a boolean.'})
        try:
            result = self.display.confirm(data['keep'], data.get('revision'))
        except DisplayError as exc:
            raise self._error(exc)
        except OSError:
            raise ApiError(409, 'display-write-failed', 'The startup card could not save the display. Check that it is writable.')
        return web.json_response(result)
