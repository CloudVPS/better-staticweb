Better Staticweb
================

An improved staticweb plugin for swift proxy. Replaces Error messages with HTML errors, and uses Jinja2 templating to render directory listings.

End user's guide
----------------
If you use your browser to  visit an objectstore with better_staticweb (BS) enabled, you'll be greeted with a nice looking directory index (assuming you're properly authenticated). This is the first feature of BS: automatic html directory listing. This is designed so it won't interfere with normal API operations, if the client (your browser) indicates it would like HTML (via the accept header) BS will provide it for you. You can control this on a per-container level by setting the `X-Container-Meta-Web-Listings` header to `auto`, `on` or `off`.

You can control the way the listing looks in one of two ways: you can provide a CSS stylesheet through the `X-Container-Meta-Listings-Css` header, or you can provide a different Jinja2 template through the `X-Container-Meta-Listings-Template` header.

Any HTTP errors produced downstream from this middleware will be caught and converted into (potentially) more user-friendly HTML errors. You can provide static pages for these errors by setting the `X-Container-Meta-Web-Error` to some suffix. Error pages are then loaded from <STATUS><Suffix>, e.g. `404error.html` if you used error.html as the suffix.

Lastly, if you set the `X-Container-Meta-Web-Index` header, that header will be served instead of a directory listing. This name will also be used for pseudo-folders, so if you set your index to 'index.html' (a common choice), `foo/index.html` will be served whenever users visit `foo/`.

Sysadmin's guide
----------------

Add the following to your proxy-server.conf:

	[filter:staticweb]
	use = egg:better_staticweb#better_staticweb
	template_path = /usr/share/better_staticweb
	powered = This objectstore is powered by <a href="http://www.cloudvps.com/">CloudVPS</a>

Add the staticweb middleware to the pipeline, prefereably before any token validation, so BS can catch 401 and 403 errors.

In /usr/share/better_staticweb, you can add XXX.html, with XXX being a HTTP status code, to provide branded error messages. At the same location, you can add `index.html`, a Jinja2 template for directory listings. The default listing is provided in `default_template.html`.
