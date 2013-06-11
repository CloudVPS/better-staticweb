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

"""
This StaticWeb WSGI middleware will serve container data as a static web site
with index file and error file resolution and optional file listings. This mode
is normally only active for anonymous requests. If you want to use it with
authenticated requests, set the ``X-Web-Mode: true`` header on the request.

The ``staticweb`` filter should be added to the pipeline in your
``/etc/swift/proxy-server.conf`` file just after any auth middleware. Also, the
configuration section for the ``staticweb`` middleware itself needs to be
added. For example::

    [DEFAULT]
    ...

    [pipeline:main]
    pipeline = catch_errors healthcheck proxy-logging cache ratelimit tempauth
               staticweb proxy-logging proxy-server

    ...

    [filter:staticweb]
    use = egg:swift#staticweb
    # Seconds to cache container x-container-meta-web-* header values.
    # cache_timeout = 300

Any publicly readable containers (for example, ``X-Container-Read: .r:*``, see
`acls`_ for more information on this) will be checked for
X-Container-Meta-Web-Index and X-Container-Meta-Web-Error header values::

    X-Container-Meta-Web-Index  <index.name>
    X-Container-Meta-Web-Error  <error.name.suffix>

If X-Container-Meta-Web-Index is set, any <index.name> files will be served
without having to specify the <index.name> part. For instance, setting
``X-Container-Meta-Web-Index: index.html`` will be able to serve the object
.../pseudo/path/index.html with just .../pseudo/path or .../pseudo/path/

If X-Container-Meta-Web-Error is set, any errors (currently just 401
Unauthorized and 404 Not Found) will instead serve the
.../<status.code><error.name.suffix> object. For instance, setting
``X-Container-Meta-Web-Error: error.html`` will serve .../404error.html for
requests for paths not found.

For pseudo paths that have no <index.name>, this middleware can serve HTML file
listings if you set the ``X-Container-Meta-Web-Listings: true`` metadata item
on the container.

If listings are enabled, the listings can have a custom style sheet by setting
the X-Container-Meta-Web-Listings-CSS header. For instance, setting
``X-Container-Meta-Web-Listings-CSS: listing.css`` will make listings link to
the .../listing.css style sheet. If you "view source" in your browser on a
listing page, you will see the well defined document structure that can be
styled.

Example usage of this middleware via ``swift``:

    Make the container publicly readable::

        swift post -r '.r:*' container

    You should be able to get objects directly, but no index.html resolution or
    listings.

    Set an index file directive::

        swift post -m 'web-index:index.html' container

    You should be able to hit paths that have an index.html without needing to
    type the index.html part.

    Turn on listings::

        swift post -m 'web-listings: true' container

    Now you should see object listings for paths and pseudo paths that have no
    index.html.

    Enable a custom listings style sheet::

        swift post -m 'web-listings-css:listings.css' container

    Set an error file::

        swift post -m 'web-error:error.html' container

    Now 401's should load 401error.html, 404's should load 404error.html, etc.
"""

from urllib import quote as urllib_quote

from swift.common.utils import cache_from_env, human_readable, split_path, \
    TRUE_VALUES, json
from swift.common.wsgi import make_pre_authed_env, make_pre_authed_request, \
    WSGIContext
from swift.common.http import is_success, is_redirection, HTTP_NOT_FOUND

import jinja2


default_template = """
<!DOCTYPE HTML PUBLIC
    "-//W3C//DTD HTML 4.01 Transitional//EN"
    "http://www.w3.org/TR/html4/loose.dtd">
<html>
 <head>
  <title>Listing of {{path}}</title>
  {% if listings_css %}
    <link rel="stylesheet" type="text/css" href="{{listings_css}}">
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
    <h1 id="title">Listing of {{path}}</h1>
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

      {% for item in items %}
      <tr class="item {{item.type_classes}}">
        <td class="colname"><a href="{{item.url}}">{{item.name}}</a></td>
        <td class="colsize">{{item.size}}</td>
        <td class="coldate">{{item.date}}</td>
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


def make_relative(path, prefix=''):
    """
    Constructs a relative path from a given prefix within the container.
    URLs and paths starting with '/' are not modified.

    :param prefix: The prefix for the container listing.
    """
    if path.startswith(('/', 'http://', 'https://')):
        css_path = quote(path, ':/')
    else:
        css_path = '../' * prefix.count('/') + quote(path)

    return css_path

class _StaticWebContext(WSGIContext):
    """
    The Static Web WSGI middleware filter; serves container data as a
    static web site. See `staticweb`_ for an overview.

    This _StaticWebContext is used by StaticWeb with each request
    that might need to be handled to make keeping contextual
    information about the request a bit simpler than storing it in
    the WSGI env.
    """

    def __init__(self, staticweb, version, account, container, obj):
        WSGIContext.__init__(self, staticweb.app)
        self.version = version
        self.account = account
        self.container = container
        self.obj = obj
        self.app = staticweb.app
        self.cache_timeout = staticweb.cache_timeout
        self.agent = '%(orig)s StaticWeb'
        # Results from the last call to self._get_container_info.
        self._container_info = None

    def _error_response(self, response, env, start_response):
        """
        Sends the error response to the remote client, possibly resolving a
        custom error response body based on x-container-meta-web-error.

        :param response: The error response we should default to sending.
        :param env: The original request WSGI environment.
        :param start_response: The WSGI start_response hook.
        """
        if not self._error:
            start_response(self._response_status, self._response_headers,
                           self._response_exc_info)
            return response

        save_response_status = self._response_status
        save_response_headers = self._response_headers
        save_response_exc_info = self._response_exc_info
        resp = self._app_call(make_pre_authed_env(
            env, 'GET', '/%s/%s/%s/%s%s' % (
                self.version, self.account, self.container,
                self._get_status_int(), self._error),
            self.agent, swift_source='SW'))
        if is_success(self._get_status_int()):
            start_response(save_response_status, self._response_headers,
                           self._response_exc_info)
            return resp
        start_response(save_response_status, save_response_headers,
                       save_response_exc_info)
        return response

    def _get_container_info(self, env):
        """
        Retrieves all x-conainer-meta-web-* headers and stores them in
        self._container_infox-container-meta-web-index, x-container-meta-web-error,
        x-container-meta-web-listings, and x-container-meta-web-listings-css
        from memcache or from the cluster and stores the result in memcache and
        in self._index, self._error, self._listings, and self._listings_css.

        :param env: The WSGI environment dict.
        """

        # Don't reget container info if it has already been retrieved.
        if self._container_info:
            return

        memcache_client = cache_from_env(env)

        if memcache_client:
            memcache_key = '/staticweb/%s/%s/%s' % (self.version, self.account,
                                                    self.container)
            cached_data = memcache_client.get(memcache_key)
            if cached_data:
                self._container_info = cached_data
                return

        resp = make_pre_authed_request(
            env, 'HEAD', '/%s/%s/%s' % (
                self.version, self.account, self.container),
            agent=self.agent, swift_source='SW').get_response(self.app)

        if is_success(resp.status_int):
            self._container_info = dict((k[17:],v) for k,v in resp.headers
                if k.lower().startswith('x-container-meta-'))

            if memcache_client:
                memcache_client.set(memcache_key, self._container_info,
                                    time=self.cache_timeout)

    def _listing(self, env, start_response, prefix=None):
        """
        Sends an HTML object listing to the remote client.

        :param env: The original WSGI environment dict.
        :param start_response: The original WSGI start_response hook.
        :param prefix: Any prefix desired for the container listing.
        """
        if self._container_info['web-listings'].lower() not in TRUE_VALUES:
            start_response("404 Not Found", [])
            return "Not found"
            # resp = HTTPNotFound()(env, self._start_response)
            # return self._error_response(resp, env, start_response)

        tmp_env = make_pre_authed_env(
            env, 'GET', '/%s/%s/%s' % (
                self.version, self.account, self.container),
            self.agent, swift_source='SW')

        tmp_env['QUERY_STRING'] = 'delimiter=/&format=json'
        if prefix:
            tmp_env['QUERY_STRING'] += '&prefix=%s' % quote(prefix)
        else:
            prefix = ''

        resp = self._app_call(tmp_env)
        if not is_success(self._get_status_int()):
            return self._error_response(resp, env, start_response)

        body = ''.join(resp)
        if body:
            listing = json.loads(body)
        if not listing:
            start_response("404 Not Found", [])
            return "Not found"
            # resp = HTTPNotFound()(env, self._start_response)
            # return self._error_response(resp, env, start_response)

        # TODO: load template from object store
        template = jinja2.Template(default_template)

        context = {
            'meta': dict((k.replace('-','_').lower(),v) for k,v in
                self._container_info),
            'at_root': not prefix,
            'prefix': prefix,
            'path': env['PATH_INFO'],
            'subdirs': [item for item in listing if ('subdir' in item)],
            'files': [item for item in listing if ('name' in item)],
        }

        for item in context['files']:
            content_type = item['content_type']
            item['type_classes'] = ' '.join('type-' + t.lower()
                                  for t in content_type.split('/'))
            item['bytes_human'] = human_readable(item['bytes'])
            item['last_modified'] = item['last_modified'].split('.')[0]. \
                            replace('T', ' ')

        # Convert css path.
        if 'web_listing_css' in context['meta']:
            path = context['meta']['web_listing_css']
            context['meta']['web_listing_css'] = make_relative(path, prefix)

        headers = {'Content-Type': 'text/html; charset=UTF-8'}

        start_response("200 OK", headers.items())
        return template.generate(context)

    def handle_container(self, env, start_response):
        """
        Handles a possible static web request for a container.

        :param env: The original WSGI environment dict.
        :param start_response: The original WSGI start_response hook.
        """
        self._get_container_info(env)

        if not self._listings and not self._index:
            if env.get('HTTP_X_WEB_MODE', 'f') in TRUE_VALUES:
                start_response("404 Not Found", [])
                return "Not found"
                # return HTTPNotFound()(env, start_response)
            return self.app(env, start_response)

        if env['PATH_INFO'][-1] != '/':
            start_response("301 Moved Permanently", [
                ('Location',env['PATH_INFO'] + '/')]
            )
            return ""
        if not self._index:
            return self._listing(env, start_response)
        tmp_env = dict(env)
        tmp_env['HTTP_USER_AGENT'] = \
            '%s StaticWeb' % env.get('HTTP_USER_AGENT')
        tmp_env['swift.source'] = 'SW'
        tmp_env['PATH_INFO'] += self._index
        resp = self._app_call(tmp_env)
        status_int = self._get_status_int()
        if status_int == HTTP_NOT_FOUND:
            return self._listing(env, start_response)
        elif not is_success(self._get_status_int()) or \
                not is_redirection(self._get_status_int()):
            return self._error_response(resp, env, start_response)
        start_response(self._response_status, self._response_headers,
                       self._response_exc_info)
        return resp

    def handle_object(self, env, start_response):
        """
        Handles a possible static web request for an object. This object could
        resolve into an index or listing request.

        :param env: The original WSGI environment dict.
        :param start_response: The original WSGI start_response hook.
        """
        tmp_env = dict(env)
        tmp_env['HTTP_USER_AGENT'] = \
            '%s StaticWeb' % env.get('HTTP_USER_AGENT')
        tmp_env['swift.source'] = 'SW'
        resp = self._app_call(tmp_env)
        status_int = self._get_status_int()
        if is_success(status_int) or is_redirection(status_int):
            start_response(self._response_status, self._response_headers,
                           self._response_exc_info)
            return resp
        if status_int != HTTP_NOT_FOUND:
            return self._error_response(resp, env, start_response)
        self._get_container_info(env)
        if not self._listings and not self._index:
            return self.app(env, start_response)
        status_int = HTTP_NOT_FOUND
        if self._index:
            tmp_env = dict(env)
            tmp_env['HTTP_USER_AGENT'] = \
                '%s StaticWeb' % env.get('HTTP_USER_AGENT')
            tmp_env['swift.source'] = 'SW'
            if tmp_env['PATH_INFO'][-1] != '/':
                tmp_env['PATH_INFO'] += '/'
            tmp_env['PATH_INFO'] += self._index
            resp = self._app_call(tmp_env)
            status_int = self._get_status_int()
            if is_success(status_int) or is_redirection(status_int):
                if env['PATH_INFO'][-1] != '/':
                    start_response("301 Moved Permanently", [
                        ('Location',env['PATH_INFO'] + '/')]
                    )
                    return ""
                start_response(self._response_status, self._response_headers,
                               self._response_exc_info)
                return resp
        if status_int == HTTP_NOT_FOUND:
            if env['PATH_INFO'][-1] != '/':
                tmp_env = make_pre_authed_env(
                    env, 'GET', '/%s/%s/%s' % (
                        self.version, self.account, self.container),
                    self.agent, swift_source='SW')
                tmp_env['QUERY_STRING'] = 'limit=1&format=json&delimiter' \
                    '=/&limit=1&prefix=%s' % quote(self.obj + '/')
                resp = self._app_call(tmp_env)
                body = ''.join(resp)
                if not is_success(self._get_status_int()) or not body or \
                        not json.loads(body):
                    start_response("404 Not Found", [])
                    return "Not found"
                    # resp = HTTPNotFound()(env, self._start_response)
                    # return self._error_response(resp, env, start_response)
                start_response("301 Moved Permanently", [
                    ('Location',env['PATH_INFO'] + '/')]
                )
                return ""
            return self._listing(env, start_response, self.obj)


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

        # Clear the cache when there is a PUT or POST to the container.
        if env['REQUEST_METHOD'] in ('PUT', 'POST') and container and not obj:
            memcache_client = cache_from_env(env)
            if memcache_client:
                memcache_key = \
                    '/staticweb/%s/%s/%s' % (version, account, container)
                memcache_client.delete(memcache_key)
            return self.app(env, start_response)

        # Don't handle non-GET requests.
        if env['REQUEST_METHOD'] not in ('HEAD', 'GET'):
            return self.app(env, start_response)

        # Don't authenticated requetsts, unless x-web-mode is True
        if env.get('REMOTE_USER') and \
                env.get('HTTP_X_WEB_MODE', 'f').lower() not in TRUE_VALUES:
            return self.app(env, start_response)

        # Don't handle account requests.
        if not container:
            return self.app(env, start_response)

        context = _StaticWebContext(self, version, account, container, obj)
        if obj:
            return context.handle_object(env, start_response)

        return context.handle_container(env, start_response)


def filter_factory(global_conf, **local_conf):
    """ Returns a Static Web WSGI filter for use with paste.deploy. """
    conf = global_conf.copy()
    conf.update(local_conf)

    def staticweb_filter(app):
        return StaticWeb(app, conf)

    return staticweb_filter
