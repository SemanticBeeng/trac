# -*- coding: utf-8 -*-
#
# Copyright (C) 2008-2018 Edgewall Software
# All rights reserved.
#
# This software is licensed as described in the file COPYING, which
# you should have received as part of this distribution. The terms
# are also available at http://trac.edgewall.org/wiki/TracLicense.
#
# This software consists of voluntary contributions made by many
# individuals. For the exact contribution history, see the revision
# history and logs, available at http://trac.edgewall.org/log/.

"""better_twill is a small wrapper around twill to set some sane defaults and
monkey-patch some better versions of some of twill's methods.
It also handles twill's absense.
"""

import io
import os
import sys
import urllib
import urlparse
from os.path import abspath, dirname, join
from pkg_resources import parse_version as pv

from trac.util.text import to_unicode

# On OSX lxml needs to be imported before twill to avoid Resolver issues
# somehow caused by the mac specific 'ic' module
try:
    from lxml import etree
except ImportError:
    pass

try:
    import twill
except ImportError:
    twill = None

# When twill tries to connect to a site before the site is up, it raises an
# exception.  In 0.9b1, it's urlib2.URLError, but in -latest, it's
# twill.browser.BrowserStateError.
try:
    from twill.browser import BrowserStateError as ConnectError
except ImportError:
    from urllib2 import URLError as ConnectError


if twill:
    # We want Trac to generate valid html, and therefore want to test against
    # the html as generated by Trac.  "tidy" tries to clean up broken html,
    # and is responsible for one difficult to track down testcase failure
    # (for #5497).  Therefore we turn it off here.
    twill.commands.config('use_tidy', '0')

    # We use a transparent proxy to access the global browser object through
    # twill.get_browser(), as the browser can be destroyed by browser_reset()
    # (see #7472).
    class _BrowserProxy(object):
        def __getattribute__(self, name):
            return getattr(twill.get_browser(), name)

        def __setattr__(self, name, value):
            setattr(twill.get_browser(), name, value)

    # setup short names to reduce typing
    # This twill browser (and the tc commands that use it) are essentially
    # global, and not tied to our test fixture.
    tc = twill.commands
    b = _BrowserProxy()

    # Setup XHTML validation for all retrieved pages
    try:
        from lxml import etree
    except ImportError:
        print("SKIP: validation of XHTML output in functional tests"
              " (no lxml installed)")
        etree = None

    if etree and pv(etree.__version__) < pv('2.0.0'):
        # 2.0.7 and 2.1.x are known to work.
        print("SKIP: validation of XHTML output in functional tests"
              " (lxml < 2.0, api incompatibility)")
        etree = None

    if etree:
        class _Resolver(etree.Resolver):
            base_dir = dirname(abspath(__file__))

            def resolve(self, system_url, public_id, context):
                return self.resolve_filename(join(self.base_dir,
                                                  system_url.split("/")[-1]),
                                             context)

        _parser = etree.XMLParser(dtd_validation=True)
        _parser.resolvers.add(_Resolver())
        etree.set_default_parser(_parser)

        def _format_error_log(data, log):
            msg = []
            for entry in log:
                context = data.splitlines()[max(0, entry.line - 5):
                                            entry.line + 6]
                msg.append("\n# %s\n# URL: %s\n# Line %d, column %d\n\n%s\n"
                           % (entry.message, entry.filename, entry.line,
                              entry.column, "\n".join(each.decode('utf-8')
                                                      for each in context)))
            return "\n".join(msg).encode('ascii', 'xmlcharrefreplace')

        def _validate_xhtml(func_name, *args, **kwargs):
            page = b.get_html()
            if "xhtml1-strict.dtd" not in page:
                return
            etree.clear_error_log()
            try:
                # lxml will try to convert the URL to unicode by itself,
                # this won't work for non-ascii URLs, so help him
                url = b.get_url()
                if isinstance(url, str):
                    url = unicode(url, 'latin1')
                etree.parse(io.BytesIO(page), base_url=url)
            except etree.XMLSyntaxError as e:
                raise twill.errors.TwillAssertionError(
                    _format_error_log(page, e.error_log))

        b._post_load_hooks.append(_validate_xhtml)

    # When we can't find something we expected, or find something we didn't
    # expect, it helps the debugging effort to have a copy of the html to
    # analyze.
    def twill_write_html():
        """Write the current html to a file.  Name the file based on the
        current testcase.
        """
        import unittest

        frame = sys._getframe()
        while frame:
            if frame.f_code.co_name in ('runTest', 'setUp', 'tearDown'):
                testcase = frame.f_locals['self']
                testname = testcase.__class__.__name__
                tracdir = testcase._testenv.tracdir
                break
            elif isinstance(frame.f_locals.get('self'), unittest.TestCase):
                testcase = frame.f_locals['self']
                testname = '%s.%s' % (testcase.__class__.__name__,
                                      testcase._testMethodName)
                tracdir = testcase._testenv.tracdir
                break
            frame = frame.f_back
        else:
            # We didn't find a testcase in the stack, so we have no clue what's
            # going on.
            raise Exception("No testcase was found on the stack.  This was "
                            "really not expected, and I don't know how to "
                            "handle it.")

        filename = os.path.join(tracdir, 'log', "%s.html" % testname)
        with open(filename, 'w') as html_file:
            html_file.write(b.get_html())

        return urlparse.urljoin('file:', urllib.pathname2url(filename))

    # Twill isn't as helpful with errors as I'd like it to be, so we replace
    # the formvalue function.  This would be better done as a patch to Twill.
    def better_formvalue(form, field, value, fv=tc.formvalue):
        try:
            fv(form, field, value)
        except (twill.errors.TwillAssertionError,
                twill.errors.TwillException,
                twill.utils.ClientForm.ItemNotFoundError) as e:
            filename = twill_write_html()
            raise twill.errors.TwillAssertionError('%s at %s' %
                                                   (unicode(e), filename))
    tc.formvalue = better_formvalue
    tc.fv = better_formvalue

    # Twill requires that on pages with more than one form, you have to click a
    # field within the form before you can click submit.  There are a number of
    # cases where the first interaction a user would have with a form is
    # clicking on a button.  This enhancement allows us to specify the form to
    # click on.
    def better_browser_submit(fieldname=None, formname=None, browser=b, old_submit=b.submit):
        if formname is not None: # enhancement to directly specify the form
            browser._browser.form = browser.get_form(formname)
        old_submit(fieldname)
    b.submit = better_browser_submit

    def better_submit(fieldname=None, formname=None):
        b.submit(fieldname, formname)
    tc.submit = better_submit

    # Twill's formfile function leaves a file handle open which prevents the
    # file from being deleted on Windows.  Since we would just assume use a
    # BytesIO object in the first place, allow the file-like object to be
    # provided directly.
    def better_formfile(formname, fieldname, filename, content_type=None,
                        fp=None):
        if not fp:
            filename = filename.replace('/', os.path.sep)
            with open(filename, 'rb') as ftemp:
                fp = io.BytesIO(ftemp.read())

        form = b.get_form(formname)
        control = b.get_form_field(form, fieldname)

        if not control.is_of_kind('file'):
            raise twill.errors.TwillException("ERROR: field is not a file "
                                              "upload field!")

        b.clicked(form, control)
        control.add_file(fp, content_type, filename)
    tc.formfile = better_formfile

    # Twill's tc.find() does not provide any guidance on what we got
    # instead of what was expected.
    def better_find(what, flags='', tcfind=tc.find):
        try:
            tcfind(what, flags)
        except twill.errors.TwillAssertionError as e:
            filename = twill_write_html()
            raise twill.errors.TwillAssertionError('%s at %s' %
                                                   (to_unicode(e), filename))
    tc.find = better_find

    def better_notfind(what, flags='', tcnotfind=tc.notfind):
        try:
            tcnotfind(what, flags)
        except twill.errors.TwillAssertionError as e:
            filename = twill_write_html()
            raise twill.errors.TwillAssertionError('%s at %s' %
                                                   (to_unicode(e), filename))
    tc.notfind = better_notfind

    # Same for tc.url - no hint about what went wrong!
    def better_url(should_be, tcurl=tc.url):
        try:
            tcurl(should_be)
        except twill.errors.TwillAssertionError as e:
            filename = twill_write_html()
            raise twill.errors.TwillAssertionError('%s at %s' %
                                                   (to_unicode(e), filename))
    tc.url = better_url
else:
    b = tc = None
