"""
Rate limiter and quota system.

Framework-independent rate limiting mechanism that provides:
    * IP address and token-based accounting
    * customizable quotas based on IP address and token
    * late limiting based on resource usage (time spent on API calls)
    * bucketing based on day, week, month
    * statistics
    * metrics
    * fast in-memory storage

Also provides a connector for Flask

"""

import time
import ipaddress
from typing import Dict, List, Optional, Tuple, Union

IpAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]
IpAddrBucket = Dict[IpAddress, float]
IpAddrBuckets = Tuple[IpAddrBucket, IpAddrBucket, IpAddrBucket]
TokenBucket = Dict[str, float]
TokenBuckets = Tuple[TokenBucket, TokenBucket, TokenBucket]


class Limiter:
    def __init__(
        self,
        limits: dict,
        token_check_callback=None,
        ipaddr_methods=["X-Real-Ip", "socket"],
        whitelisted_ipaddrs=Optional[List[str]],
    ):
        # Bucket sequence: month, week, day
        self._ipaddr_limits = [
            limits.get(l, None)
            for l in ("ipaddr_per_month", "ipaddr_per_week", "ipaddr_per_day")
        ]
        self._token_limits = [
            limits.get(l, None)
            for l in ("token_per_month", "token_per_week", "token_per_day")
        ]
        self._ipaddr_buckets = ({}, {}, {})  # type: IpAddrBuckets
        self._token_buckets = ({}, {}, {})  # type: TokenBuckets
        self._token_check_callback = token_check_callback
        self._ipaddr_extraction_methods = ipaddr_methods
        self._last_quota_update_time = time.monotonic()
        self._whitelisted_ipaddrs = set()
        for ipa in whitelisted_ipaddrs or []:
            self._whitelisted_ipaddrs.add(ipaddress.ip_address(ipa))

        self.increment_quota_counters(1)
        self.refresh_quota_counters_if_needed()

    def increment_quota_counters(self, tdelta: int):
        """Delta: time from previous run in seconds"""
        if tdelta <= 0:
            return

        iterable = (
            (30 * 24, self._ipaddr_limits[0], self._ipaddr_buckets[0]),
            (7 * 24, self._ipaddr_limits[0], self._ipaddr_buckets[0]),
            (1 * 24, self._ipaddr_limits[0], self._ipaddr_buckets[0]),
            (30 * 24, self._ipaddr_limits[0], self._ipaddr_buckets[0]),
            (7 * 24, self._ipaddr_limits[0], self._ipaddr_buckets[0]),
            (1 * 24, self._ipaddr_limits[0], self._ipaddr_buckets[0]),
        )
        for hours, limit, bucket in iterable:
            vdelta = limit / hours / 3600 * tdelta
            to_delete = []
            for k, v in bucket.items():
                v += vdelta
                if v >= limit:
                    to_delete.append(k)
                else:
                    bucket[k] = v

            for k in to_delete:
                del bucket[k]

    def refresh_quota_counters_if_needed(self):
        t = time.monotonic()
        delta = t - self._last_quota_update_time
        if delta > 3600:
            self.increment_quota_counters(delta)

        self._last_quota_update_time = t

    def consume_quota(self, elapsed: float, ipaddr: Optional[IpAddress]=None, token=None) -> None:
        """Consume quota in seconds
        """
        assert ipaddr or token
        if ipaddr:
            assert isinstance(ipaddr, ipaddress.IPv4Address)
            for n, limit in enumerate(self._ipaddr_limits):
                b = self._ipaddr_buckets[n]
                b[ipaddr] = b.get(ipaddr, limit) - elapsed

        else:
            raise NotImplementedError()

    def get_minimum_across_quotas(self, ipaddr=None, token=None) -> float:
        assert ipaddr or token
        if ipaddr:
            iterable = zip(self._ipaddr_limits, self._ipaddr_buckets)
            return min(bucket.get(ipaddr, limit) for limit, bucket in iterable)

        else:
            raise NotImplementedError()

    def is_quota_available(self, ipaddr=None, token=None) -> bool:
        """Check if all quota buckets for an ipaddr/token are > 0
        """
        # return False if any bucket reached 0
        for bucket in self._ipaddr_buckets:
            if ipaddr in bucket:
                if bucket[ipaddr] <= 0:
                    return False

        return True

    def is_ipaddr_whitelisted(self, ipaddr: IpAddress) -> bool:
        return ipaddr in self._whitelisted_ipaddrs

    def get_lowest_daily_quotas_summary(self, n=20) -> List[Tuple[int, float]]:
        """Returns a summary of daily quotas with the lowest values
        """
        li = sorted((val, ipa) for ipa, val in self._ipaddr_buckets[2].items())
        li = li[:n]
        return [(int(ipa.packed[0]), val) for val, ipa in li]


# # Flask-specific code # #

from flask import request, current_app
import flask


class FlaskLimiter:
    def _get_client_ipaddr(self) -> IpAddress:
        # https://github.com/alisaifee/flask-limiter/issues/41
        for m in self._limiter._ipaddr_extraction_methods:
            if m == "X-Forwarded-For":
                raise NotImplementedError("X-Forwarded-For ")

            elif m == "X-Real-Ip":
                ipaddr = request.headers.get("X-Real-Ip", None)
                if ipaddr:
                    return ipaddress.ip_address(ipaddr)

            elif m == "socket":
                return ipaddress.ip_address(request.remote_addr)

            else:
                raise NotImplementedError(f"IP address method {m} is unknown")

        methods = ",".join(self._limiter._ipaddr_extraction_methods)
        raise Exception(f"Unable to detect IP address using {methods}")

    def _check_limits_callback(self):
        """Check rate limits before processing a request
        Refresh quota counters when needed
        """
        self._limiter.refresh_quota_counters_if_needed()
        ipaddr = self._get_client_ipaddr()
        # token = request.headers.get("Token", None)
        # if token:
        # check token validity
        if not self._limiter.is_quota_available(ipaddr=ipaddr):
            flask.abort(429)
        self._request_start_time = time.monotonic()
        log = current_app.logger
        log.error("_check_limits_callback called")

    def _after_request_callback(self, response):
        """Consume quota and injects HTTP headers when responding to a request
        """
        log = current_app.logger
        try:
            assert response
            tdelta = time.monotonic() - self._request_start_time
            ipaddr = self._get_client_ipaddr()
            if not self._limiter.is_ipaddr_whitelisted(ipaddr):
                self._limiter.consume_quota(tdelta, ipaddr=ipaddr)
                q = self._limiter.get_minimum_across_quotas(ipaddr=ipaddr)
                response.headers.add("X-RateLimit-Remaining", q)

        except Exception as e:
            log.error(str(e), exc_info=True)

        finally:
            return response

    def __init__(
        self,
        app,
        limits: dict,
        token_check_callback=None,
        ipaddr_methods=["X-Real-Ip", "socket"],
        whitelisted_ipaddrs=None,
    ):
        """
        """
        self._limiter = Limiter(
            limits,
            token_check_callback=token_check_callback,
            ipaddr_methods=ipaddr_methods,
            whitelisted_ipaddrs=whitelisted_ipaddrs,
        )
        if app.extensions.get("limiter"):
            raise Exception("The Flask app already has an extension named 'limiter'")

        app.before_request(self._check_limits_callback)
        app.after_request(self._after_request_callback)
        app.extensions["limiter"] = self

    def get_lowest_daily_quotas_summary(self, n=20) -> List[Tuple[int, float]]:
        return self._limiter.get_lowest_daily_quotas_summary(n)
