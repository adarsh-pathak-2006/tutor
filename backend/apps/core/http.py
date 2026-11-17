from django.conf import settings
from django.http import StreamingHttpResponse


class AsyncStreamingHttpResponse(StreamingHttpResponse):
    def __init__(self, stream, *args, **kwargs):
        self._stream = stream
        super().__init__(aiter(stream) if hasattr(stream, "__aiter__") else stream, *args, **kwargs)

    @property
    def streaming_content(self):
        stream = self._stream

        class HybridContent:
            def __iter__(self_inner):
                if hasattr(stream, "__iter__"):
                    return map(self.make_bytes, stream)
                return iter([])

            def __aiter__(self_inner):
                if hasattr(stream, "__aiter__"):

                    async def _aiter():
                        async for item in stream:
                            yield self.make_bytes(item)

                    return _aiter()
                return aiter([])

        return HybridContent()

    @streaming_content.setter
    def streaming_content(self, value):
        StreamingHttpResponse.streaming_content.fset(self, value)


def client_ip(request):
    """The client's IP address, trusting X-Forwarded-For only as far as our own proxies.

    Each proxy appends the address it received the request from, so with N trusted proxies the real
    client is the Nth entry from the right. Anything further left was written by the client and can be
    forged, so it is never used.
    """
    remote_addr = request.META.get("REMOTE_ADDR")
    proxies = settings.TUTOR_TRUSTED_PROXIES
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if proxies <= 0 or not forwarded:
        return remote_addr
    addresses = [a.strip() for a in forwarded.split(",") if a.strip()]
    if not addresses:
        return remote_addr
    return addresses[-min(proxies, len(addresses))]
