#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import os
import os.path as osp
import re
import sys
import tempfile
import time
from email.message import Message
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlsplit

import cloudscraper
import requests
import tqdm

CHUNK_SIZE = 512 * 1024  # 512KB
REQUEST_TIMEOUT = (10, 60)


class DownloadError(RuntimeError):
    pass


class _MediaFireSession(cloudscraper.CloudScraper):
    def request(self, *args, **kwargs):
        # 3.0.0 leaks this counter when a challenge handler returns early.
        active_requests = self.current_concurrent_requests
        try:
            return super().request(*args, **kwargs)
        finally:
            self.current_concurrent_requests = active_requests

    def _refresh_session(self, url):
        # upstream's Session.get() calls self.request() again, recursively refreshing.
        self._clear_cloudflare_cookies()
        self.session_start_time = time.time()
        self.request_count = 0
        self.user_agent.loadUserAgent()
        self.headers.update(self.user_agent.headers)
        try:
            with self.perform_request('GET', urljoin(url, '/'), timeout=REQUEST_TIMEOUT) as response:
                refreshed = response.status_code in (200, 301, 302, 304)
        except requests.RequestException:
            return False
        if refreshed:
            self.last_403_time = 0
        return refreshed


def _create_session():
    # Challenge handling makes nested requests; the default limit of 1 deadlocks.
    # Keep the generated headers so they still match the browser profile.
    return _MediaFireSession.create_scraper(
        interpreter='js2py', delay=5,
        browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False},
        solveDepth=3, max_403_retries=3, max_concurrent_requests=8,
    )


class _DownloadLinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.button_link = None
        self.fallback_link = None

    def handle_starttag(self, tag, attrs):
        if tag != 'a':
            return
        attrs = dict(attrs)
        href = attrs.get('href') or ''
        if not href or href.startswith(('#', 'javascript:')):
            return
        if attrs.get('id') == 'downloadButton':
            self.button_link = href
        if self.fallback_link is None and re.match(r'https?://download[^/]+/', href):
            self.fallback_link = href


def extractDownloadLink(contents):
    parser = _DownloadLinkParser()
    parser.feed(contents)
    return parser.button_link or parser.fallback_link


def _open_download(sess, url):
    for _ in range(5):
        res = sess.get(url, stream=True, timeout=REQUEST_TIMEOUT)
        try:
            if res.headers.get('cf-mitigated') == 'challenge':
                raise DownloadError(
                    'Cloudflare returned a browser verification page (HTTP %s). '
                    'The current session could not complete verification.' % res.status_code
                )
            res.raise_for_status()
            if 'Content-Disposition' in res.headers:
                return res
            contents = res.text
            link = extractDownloadLink(contents)
            if link is None:
                if 'Just a moment...' in contents or '/cdn-cgi/challenge-platform/' in contents:
                    raise DownloadError('Cloudflare browser verification is still required.')
                raise DownloadError(
                    'No download link found at %s. The file may be unavailable '
                    'or the page format may have changed.' % res.url
                )
            url = urljoin(res.url, link)
            if urlsplit(url).scheme not in ('http', 'https'):
                raise DownloadError('The download button did not contain an HTTP URL.')
        except BaseException:
            res.close()
            raise
        res.close()
    raise DownloadError('Too many download-page redirects.')


def download(url, output=None, quiet=False):
    with _create_session() as sess, _open_download(sess, url) as res:
        if output is None:
            disposition = Message()
            disposition['Content-Disposition'] = res.headers['Content-Disposition']
            filename = disposition.get_filename() or unquote(urlsplit(res.url).path.rsplit('/', 1)[-1])
            output = osp.basename(filename.replace('\\', '/'))
            if output in ('', '.', '..'):
                raise DownloadError('No usable filename found; specify one with -o.')

        output_is_path = isinstance(output, (str, os.PathLike))
        if output_is_path:
            output = os.fspath(output)
        if not quiet:
            print('Downloading...', file=sys.stderr)
            print('From:', url, file=sys.stderr)
            print('To:', osp.abspath(output) if output_is_path else output, file=sys.stderr)

        if output_is_path:
            fd, tmp_file = tempfile.mkstemp(
                suffix='.part', prefix=osp.basename(output) + '.',
                dir=osp.dirname(output) or '.',
            )
            f = os.fdopen(fd, 'wb')
        else:
            tmp_file, f = None, output

        total_header = res.headers.get('Content-Length', '')
        total = int(total_header) if total_header.isdigit() else None
        pbar = None
        try:
            pbar = tqdm.tqdm(total=total, unit='B', unit_scale=True, disable=quiet)
            written = 0
            for chunk in res.iter_content(chunk_size=CHUNK_SIZE):
                f.write(chunk)
                written += len(chunk)
                pbar.update(len(chunk))
            if total is not None and not res.headers.get('Content-Encoding') and written != total:
                raise DownloadError('Incomplete download: expected %s bytes, received %s.' % (total, written))
            if tmp_file:
                f.close()
                os.replace(tmp_file, output)
        finally:
            if pbar is not None:
                pbar.close()
            if tmp_file:
                f.close()
                if osp.exists(tmp_file):
                    os.remove(tmp_file)
    return output


def main():
    desc = 'Simple command-line script to download files from mediafire'
    parser = argparse.ArgumentParser(description=desc)
    parser.add_argument('url', nargs='+')
    parser.add_argument('-o', '--output', help='output filename')
    args = parser.parse_args()

    if len(args.url) > 1 and args.output:
        parser.error('-o/--output can only be used with a single URL')
    failed = False
    for url in args.url:
        try:
            download(url, output=args.output, quiet=False)
        except (DownloadError, requests.RequestException, OSError) as error:
            print('Download failed: %s' % error, file=sys.stderr)
            failed = True
    return 1 if failed else 0

if __name__ == "__main__":
    sys.exit(main())
