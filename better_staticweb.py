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

from swift.common.utils import cache_from_env, split_path, json
from StringIO import StringIO

import jinja2
import itertools

default_template = """
<!DOCTYPE HTML PUBLIC
    "-//W3C//DTD HTML 4.01 Transitional//EN"
    "http://www.w3.org/TR/html4/loose.dtd">
<html>
 <head>
  <title>Listing of {{path|e}}</title>
  {% if listings_css %}
    <link rel="stylesheet" type="text/css" href="{{listings_css|e}}">
  {% else %}
    <style>
       h1 {font-size: 1em; font-weight: bold;}
       th {text-align: left; padding: 0px 1em 0px 1em;}
       td {padding: 0px 1em 0px 1em;}
       a {text-decoration: none;}
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


    def _get_container_info(self, env, account, container):
        """
        Retrieves all x-conainer-meta-web-* headers and stores them in
        self._container_infox-container-meta-web-index, x-container-meta-web-error,
        x-container-meta-web-listings, and x-container-meta-web-listings-css
        from memcache or from the cluster and stores the result in memcache and
        in self._index, self._error, self._listings, and self._listings_css.

        :param env: The WSGI environment dict.
        """

        if not container: # No configurable items in account
            return {}

        if self._cache:
            memcache_key = 'better_static/%s/%s' % (account, container )
            cached_data = self._cache.get(memcache_key)
            if cached_data:
                return cached_data

        status, headers, content = self.do_internal_get(
            env,
            '/v1/%s/%s' % (account, container),
            method='HEAD',
            preauthenticate=True
        ).get_response(self.app)

        if 200 <= int(status[:3]) < 300:
            result = dict(
                (k[17:],v) for k,v in headers
                if k.lower().startswith('x-container-meta-')
            )

            if self._cache:
                self._cache.set(memcache_key, result,
                                timeout=self.cache_timeout)

            return result

        return {}

    def do_internal_get(self, env, path, method="GET", preauthenticate=False):
        tmp_env = dict(env)
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

        if not isinstance(answer[2],basestring):
            answer[2] = "".join(answer[2])

        if isinstance(answer[1], dict):
            answer[1] = answer[1].items()

        return answer


    def handle_object(self, env, start_response, account, container, obj):

        found_status = []

        def catch_404(status, headers, exc_info=None):
            found_status.append(status)
            found_status.append(headers)
            if exc_info:
                found_status.append(exc_info)

        answer = self.app(env, catch_404)

        if not found_status[0]:
            it = iter(answer)
            first = it.next()
            answer = itertools.chain([first], it)

        if found_status[0].startswith("404 "):
            backend_url = "/v1/%s/%s?delimiter=/&format=json&prefix=%s/" % (
                account, container, obj
            )

            status, headers, content = self.do_internal_get(env, backend_url)

            if not status.startswith('404 '):
                redirect_to = (env.get('HTTP_ORIGINAL_PATH') or env['PATH_INFO']) + '/'
                start_response("302 Found", [("location", redirect_to)])
                return ""

        start_response(*found_status)
        return answer

    def handle_container(self, env, start_response, account, container, prefix=""):
        backend_url = "/v1/%s/%s?delimiter=/&format=json" % (account, container)
        if prefix:
            backend_url += "&prefix=" + prefix

        status, headers, content = self.do_internal_get(env, backend_url)

        if 200 <= int(status[:3]) < 300:
            content = json.loads(content)

            template = jinja2.Template(default_template)

            context = {
                'meta': {},
                'at_root': False if prefix else True,
                'prefix': prefix,
                'path': env.get('HTTP_ORIGINAL_PATH') or env['PATH_INFO'],
                'subdirs': [item for item in content if 'subdir' in item],
                'files': [item for item in content if 'name' in item],
            }

            if prefix:
                for subdir in context['subdirs']:
                    subdir['subdir'] = subdir['subdir'][len(prefix):]
                for fil in context['files']:
                    fil['name'] = fil['name'][len(prefix):]

            headers = {'Content-Type': 'text/html; charset=UTF-8'}

            start_response("200 OK", headers.items())
            return template.generate(context)
        else:
            print "Returning " + status
            start_response(status, headers)
            return [content]


    def handle_account(self, env, start_response, account):
        status, headers, content = self.do_internal_get(env, "/v1/%s?format=json" % account)

        if 200 <= int(status[:3]) < 300:
            content = json.loads(content)

            template = jinja2.Template(default_template)

            context = {
                'meta': {},
                'at_root': True,
                'prefix': True,
                'path': '/',
                'subdirs': [{"subdir": item["name"]} for item in content],
                'files': [],
            }

            headers = {'Content-Type': 'text/html; charset=UTF-8'}

            start_response("200 OK", headers.items())
            return template.generate(context)
        else:
            print "Returning " + status
            start_response(status, headers)
            return [content]


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

        # Don't handle non-GET requests.
        if env['REQUEST_METHOD'] not in ('HEAD', 'GET'):

            # flush cache if we expect the container metadata being changed.
            if container and not obj and self._cache:
                memcache_key = 'better_static/%s/%s' % (account, container)
                self._cache.delete(memcache_key)

            return self.app(env, start_response)

        if obj and not obj.endswith('/'):
            return self.handle_object(env, start_response, account, container, obj)
        elif container:
            return self.handle_container(env, start_response, account, container, obj or "")
        else:
            return self.handle_account(env, start_response, account)


def filter_factory(global_conf, **local_conf):
    """ Returns a Static Web WSGI filter for use with paste.deploy. """
    conf = global_conf.copy()
    conf.update(local_conf)

    def staticweb_filter(app):
        return StaticWeb(app, conf)

    return staticweb_filter
