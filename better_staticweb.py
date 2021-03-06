# -*- coding: utf-8 -*-
# Copyright (c) 2010-2012 OpenStack, LLC.
# Copyright (c) 2013 CloudVPS
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from urllib import quote as urllib_quote

from swift.common.swob import Response
from swift.common.utils import cache_from_env, split_path, json, human_readable

from StringIO import StringIO

import jinja2
import itertools
import urlparse
import os.path

from swift.proxy.controllers.base import get_container_info

default_template = """
<!DOCTYPE HTML PUBLIC
    "-//W3C//DTD HTML 4.01 Transitional//EN"
    "http://www.w3.org/TR/html4/loose.dtd">
<html>
 <head>
  <title>Listing of {{path|e}}</title>
  {% if meta.listings_css %}
    <link rel="stylesheet" type="text/css" href="{{meta.listings_css|e}}">
  {% else %}
    <style>
      html { margin:0px;padding:0px; font-family: verdana, sans-serif;}
      body {    width:640px; margin:auto; }
      a {color:inherit;text-decoration:none;}
      a:hover { color:#057ec1; text-decoration: underline;}
      h1 { margin:0; padding:24px 0 16px; font-size: 28px;}

      table { padding:0px; border-collapse:collapse; width:100%; }
      th { text-align:left; color: #666; border-bottom:1px solid #777;}
      td { padding-right:24px; height:32px; }
      td.colsize { font-size:0.9em; text-align:right;}
      td.coldate { font-size:0.85em; }
    </style>
  {% endif %}
  </head>
  <body>
    <h1 id="title">Listing of {{path|e}}</h1>
    <table id="listing">
      <tr id="heading">
        <th class="colname">Name</th>
        <th class="colsize">Size</th>
        <th class="coldate">Date</th>
      </tr>

      {% if not at_root %}
      <tr id="parent" class="item">
        <td class="colname"><a href="../">../</a></td>
        <td class="colsize">&nbsp;</td>
        <td class="coldate">&nbsp;</td>
      </tr>
      {% endif %}

      {% for subdir in subdirs %}
      <tr id="parent" class="item">
        <td class="colname"><a href="{{subdir.subdir|e}}">{{subdir.subdir|e}}</a></td>
        <td class="colsize">&nbsp;</td>
        <td class="coldate">&nbsp;</td>
      </tr>
      {% endfor %}

      {% for file in files %}
      <tr class="item {{file.type_classes|e}}">
        <td class="colname"><a href="{{file.name|e}}">{{file.name|e}}</a></td>
        <td class="colsize">{{file.size}}</td>
        <td class="coldate">{{file.date}}</td>
      </tr>
      {% endfor %}
    </table>
  </body>
</html>
"""


def quote(value, safe='/'):
    """
    Patched version of urllib.quote that encodes utf-8 strings before quoting
    """
    if isinstance(value, unicode):
        value = value.encode('utf-8')
    return urllib_quote(value, safe)


class StaticWeb(object):

    """
    The Static Web WSGI middleware filter; serves container data as a static
    web site. See `staticweb`_ for an overview.

    The proxy logs created for any subrequests made will have swift.source set
    to "SW".

    :param app: The next WSGI application/filter in the paste.deploy pipeline.
    :param conf: The filter configuration dict.
    """

    def __init__(self, app, conf):
        #: The next WSGI application/filter in the paste.deploy pipeline.
        self.app = app
        #: The filter configuration dict.
        self.conf = conf
        #: The seconds to cache the x-container-meta-web-* headers.,
        self.cache_timeout = int(conf.get('cache_timeout', 300))

        self._cache = None

    def __call__(self, env, start_response):
        """
        Main hook into the WSGI paste.deploy filter/app pipeline.

        :param env: The WSGI environment dict.
        :param start_response: The WSGI start_response hook.
        """
        try:
            (version, account, container, obj) = \
                split_path(env['PATH_INFO'], 2, 4, True)
        except ValueError:
            return self.app(env, start_response)

        if not self._cache:
            self._cache = cache_from_env(env)

        # Don't handle non-GET requests or subrequests by other middleware.
        if env['REQUEST_METHOD'] not in ('HEAD', 'GET') or env.get('swift.source', None) != None:

            # flush cache if we expect the container metadata being changed.
            if container and not obj and self._cache:
                memcache_key = 'better_static/%s/%s' % (account, container)
                self._cache.delete(memcache_key)

            return self.app(env, start_response)

        # If non-html was explicitly requested, don't bother trying to format
        # the html
        params = urlparse.parse_qs(env.get('QUERY_STRING', ''))

        if 'format' in params and params['format'] != ['html']:
            return self.app(env, start_response)

        context = Context(self, env, account, container, obj)
        return context(env, start_response)


def human_readable_size(value):
    """
    Returns the byte size in a human readable format; for example 1048576 = "1Mb".
    """
    value = float(value)

    suffixes = ['byte', 'Kb', 'Mb', 'Gb', 'Tb', 'Pb', 'Eb', 'Zb', 'Yb']
    for suffix in suffixes:
        if value < 1024:
            return ("%.0f" % value), suffix

        value /= 1024.0

    return ("%.0f" % (value * 1024.0)), suffixes[-1]


class Context(object):

    def __init__(self, outer, env, account, container, obj):
        self.app = outer.app
        self.conf = outer.conf
        self.cache_timeout = outer.cache_timeout
        self._cache = outer._cache
        self.account = account
        self.container = container
        self.obj = obj
        self.env = env
        self._container_info = None

    def do_internal_get(self, path, method="GET", preauthenticate=False):

        tmp_env = dict(self.env)
        tmp_env['REQUEST_METHOD'] = "GET"
        if '?' in path:
            tmp_env['PATH_INFO'], tmp_env['QUERY_STRING'] = path.split('?', 1)
        else:
            tmp_env['PATH_INFO'] = path
            tmp_env['QUERY_STRING'] = ""

        tmp_env['SCRIPT_NAME'] = ''
        tmp_env['wsgi.input'] = StringIO('')

        if preauthenticate:
            tmp_env['swift.authorize'] = lambda req: None
            tmp_env['swift.authorize_override'] = True
            tmp_env['REMOTE_USER'] = '.wsgi.pre_authed'

        answer = [None, None, None]

        def catch_result(status, headers, exc_info=None):
            answer[0] = status
            answer[1] = headers

        answer[2] = self.app(tmp_env, catch_result)

        if not isinstance(answer[2], basestring):
            answer[2] = "".join(answer[2])

        if isinstance(answer[1], dict):
            answer[1] = answer[1].items()

        return answer

    def forward_request(self, env=None):
        """ Forwards the request to the backend, and returns the start_response
        parameters, along with the iterable response """

        found_status = []

        def catch_status(status, headers, exc_info=None):
            found_status.extend((status, headers))
            if exc_info:
                found_status.append(exc_info)

        answer = self.app(env or self.env, catch_status)

        if not found_status:
            it = iter(answer)
            first = it.next()
            answer = itertools.chain([first], it)

        assert found_status

        return found_status, answer

    def _get_container_info(self):
        """
        Retrieves all x-container-meta-web-* headers, and return them as a dict.
        """

        if not self.container:  # No configurable items in account
            return {}

        if self._container_info:
            return self._container_info

        self._container_info = get_container_info(self.env, self.app,
                                                  swift_source='BSW') or {}
        return self._container_info

    def error_response(self, status, headers, start_response):
        """
        Sends the error response to the remote client, possibly resolving a
        custom error response body based on x-container-meta-web-error.

        :param status: The error status we're responding to
        :param headers: The headers of that error status. May include headers
            like www-authenticate, which the client needs to respond properly.
        :param start_response: The WSGI start_response hook.
        """

        # Remove content-related headers, as we'll not be sending the
        # associated content anyway.
        headers = [
            (k, v) for k, v in headers if
            not k.lower().startswith("content-")
        ]

        # Lets see if X-Container-Meta-Web-Error was set.
        container_info = self._get_container_info()
        web_error = container_info.get('meta', {}).get('web-error')
        if web_error:
            err_status, err_headers, err_content = self.do_internal_get(
                "/v1/%s/%s/%s%s" % (self.account, self.container,
                                    status[:3], web_error),
                preauthenticate=True
            )

            # If the error page handler is found, use it.
            if err_status[:3] == '200':
                # Merge the headers.
                headers.extend(err_headers)
                start_response(status, err_headers)
                return err_content

        # Try to find a local handler.
        local_path = os.path.join(
            self.conf.get('template_path', __file__),
            status[:3] + '.html'
        )

        try:
            with open(local_path, 'r') as f:
                contents = f.read()

            headers.extend([
                ('content-type', 'text/html; charset=UTF-8'),
                ('content-length', str(len(contents))),
            ])
            start_response(status, headers)
            return contents

        except IOError:
            pass

        # No local handler was found. Create a new html page with the status
        # code.
        headers.extend([
            ('content-type', 'text/html; charset=UTF-8'),
        ])
        start_response(status, headers)
        return ["<html><body><h1>", status, "</h1></body></html>"]

    def handle_object(self, start_response, use_preauth):
        status, contents = self.forward_request()

        if status[0].startswith("404 "):
            # Object doesn't exist. Try to see if there are any subobjects. If
            # so, redirect to this location with a trailing slash, so it can be
            # treated like a subdirectory.
            backend_url = "/v1/%s/%s?delimiter=/&format=json&prefix=%s/" % (
                self.account, self.container, self.obj
            )

            status_inner, headers_inner, contents_inner = self.do_internal_get(
                backend_url, preauthenticate=use_preauth)

            if len(contents_inner) > 2:
                # Subobjects were found. treat this like a directory.
                redirect_to = '/v1/%s/%s/%s/' % (
                    self.account, self.container, self.obj)

                qs = self.env.get('QUERY_STRING')
                if qs:
                    redirect_to += "?" + qs

                start_response("302 Found", [("location", redirect_to)])
                return ""

        start_response(*status)

        return contents

    def handle_container(self, start_response, use_preauth):
        backend_url = "/v1/%s/%s?delimiter=/&format=json" % (
            self.account,
            self.container
        )
        if self.obj:
            backend_url += "&prefix=" + self.obj

        status, headers, content = self.do_internal_get(
            backend_url, preauthenticate=use_preauth)

        if 200 <= int(status[:3]) < 300:
            content = json.loads(content)

            container_info = self._get_container_info()

            context = {
                'meta': dict(
                    (k.replace('-', '_'), v)
                    for (k, v) in container_info.get('meta', {}).items()),
                'prefix': self.obj,
                'path': self.env.get('HTTP_ORIGINAL_PATH') or self.env['PATH_INFO'],
                'subdirs': [item for item in content if 'subdir' in item],
                'files': [item for item in content if 'name' in item],
            }

            #
            if self.obj:
                for subdir in context['subdirs']:
                    subdir['subdir'] = subdir['subdir'][len(self.obj):]
                for fil in context['files']:
                    fil['name'] = fil['name'][len(self.obj):]

            return self.mklisting(context, start_response)
        else:

            start_response(status, headers)
            return [content]

    def handle_account(self, start_response):
        status, headers, content = self.do_internal_get(
            "/v1/%s?format=json" % self.account
        )

        if 200 <= int(status[:3]) < 300:
            content = json.loads(content)

            context = {
                'meta': {},
                'prefix': '',
                'path': '/',
                'subdirs': content,
                'files': [],
            }

            return self.mklisting(context, start_response)
        else:
            start_response(status, headers)
            return [content]

    def mklisting(self, listing, start_response):

        container_info = self._get_container_info().get('meta', {})

        # Load the template
        template_name = container_info.get('web-listings-template')

        if template_name and template_name != '-':
            if template_name.startswith("../"):
                template_path = "/v1/%s/%s" % (self.account, template_name[3:])
            else:
                template_path = "/v1/%s/%s/%s" % (
                    self.account, self.container, template_name)

            # TODO: ponder whether this should be preauthenticated
            status, headers, answer = self.do_internal_get(template_path)
            if status[0] == '2':
                template = answer
            else:
                # Forward any errors.
                start_response(status, headers)
                return answer

        else:
            template = default_template

            # Try to find a local handler.
            local_path = os.path.join(
                self.conf.get('template_path', __file__),
                "index.html")

            try:
                with open(local_path, 'r') as f:
                    template = f.read()
            except IOError:
                pass

        for subdir in listing['subdirs']:
            if 'bytes' in subdir:
                subdir['size'] = human_readable(subdir['bytes'])
                subdir['size_num'], subdir[
                    'size_unit'] = human_readable_size(subdir['bytes'])

            subdir.setdefault('subdir', subdir.get('name'))

        for fil in listing['files']:
            fil['size'] = human_readable(fil['bytes'])
            fil['size_num'], fil[
                'size_unit'] = human_readable_size(fil['bytes'])
            fil['date'] = fil['last_modified']
            fil['type_classes'] = " ".join(
                ('type-%s' % t.replace(".", '-'))
                for t in fil['content_type'].split('/'))
            if '.' in fil['name']:
                fil['type_classes'] += " ext-" + fil['name'].rsplit('.', 1)[-1]

        headers = {'Content-Type': 'text/html; charset=UTF-8'}

        template_engine = jinja2.Template(template)

        listing.setdefault('at_root', listing['path'].count('/') <= 1)
        listing.setdefault('account', self.account)
        listing.setdefault('container', self.container)
        listing.setdefault('object', self.obj)

        listing.setdefault('powered', self.conf.get("powered", ''))
        listing.setdefault('authenticated', any(
            (header in self.env) for header in
            ['HTTP_AUTHORIZATION', 'HTTP_X_AUTH_TOKEN'])
        )

        try:
            html = template_engine.render(listing)
        except Exception, e:
            html = "Could not generate listing<br> %s" % str(e)

        resp = Response(headers=headers, body=html)
        return resp(self.env, start_response)

    def __call__(self, env, start_response):
        """
        Main hook into the WSGI paste.deploy filter/app pipeline.

        :param env: The WSGI environment dict.
        :param start_response: The WSGI start_response hook.
        """

        self.want_html = "text/html" in env.get('HTTP_ACCEPT', '')
        self.is_authenticated = any(
            auth_header in env
            for auth_header in ("HTTP_X_AUTH_TOKEN", "HTTP_AUTHORIZATION",
                                'HTTP_X_STORAGE_USER', 'HTTP_X_AUTH_USER')
        )

        container_info = self._get_container_info().get('meta', {})

        if not container_info.get('web-error') and not self.want_html:
            # don't bother trying to inject HTML error pages if the client
            # didn't ask for HTML in the first place.
            return self.dispatch(start_response)

        self.env = env

        found_status = []

        def catch_status(status, headers, exc_info=None):
            found_status.extend((status, headers))
            if exc_info:
                found_status.append(exc_info)

        answer = self.dispatch(catch_status)

        if not found_status:
            it = iter(answer)
            first = it.next()
            answer = itertools.chain([first], it)

        assert found_status

        if int(found_status[0][:3]) >= 400:
            return self.error_response(found_status[0], found_status[1],
                                       start_response)

        else:
            start_response(*found_status)
            return answer

    def dispatch(self, start_response):
        container_info = self._get_container_info().get('meta', {})
        if self.container:
            if self.env['PATH_INFO'].endswith('/'):
                web_index = container_info.get('web-index')
                if web_index:
                    tmp_env = dict(self.env)
                    tmp_env['PATH_INFO'] += web_index
                    return self.app(tmp_env, start_response)
            elif not self.obj and self.want_html:
                redirect_to = '/v1/%s/%s/' % (self.account, self.container)
                qs = self.env.get('QUERY_STRING')
                if qs:
                    redirect_to += "?" + qs

                start_response("302 Found", [("location", redirect_to)])
                return ""

        # if listings are explicitly enabled or disabled, follow that
        listings = container_info.get('web-listings', 'auto').lower()

        if listings in ('false', 'no', '0', 'off'):
            have_listings = False
            use_preauth = False
        elif listings in ('true', 'yes', '1', 'on'):
            have_listings = self.want_html if self.is_authenticated else True
            use_preauth = True
        else:
            have_listings = self.want_html
            use_preauth = False

        # don't bother creating an html-index if the client doesn't like HTML
        # in the first place.
        if have_listings:
            if self.obj and not self.obj.endswith('/'):
                return self.handle_object(start_response, use_preauth)
            elif self.container:
                return self.handle_container(start_response, use_preauth)
            else:
                return self.handle_account(start_response)
        else:
            return self.app(self.env, start_response)


def filter_factory(global_conf, **local_conf):
    """ Returns a Static Web WSGI filter for use with paste.deploy. """
    conf = {
        "powered": "Powered by <a href=http://swift.openstack.org>Openstack Swift</a>",
        "template_path": "/usr/share/better_staticweb/",
    }

    conf.update(global_conf)
    conf.update(local_conf)

    def staticweb_filter(app):
        return StaticWeb(app, conf)

    return staticweb_filter
