# coding=utf-8
import json
import os
import shutil

from urllib.parse import urlparse

from django.conf import settings
from django.core.cache import cache
from django.test.utils import override_settings
from django.utils.encoding import force_text
from django.utils.http import http_date, quote_etag

from unittest.mock import patch
from pyquery import PyQuery as pq

from olympia import amo
from olympia.addons.models import Addon
from olympia.amo.tests import TestCase, version_factory, addon_factory
from olympia.amo.urlresolvers import reverse
from olympia.files.file_viewer import DiffHelper, FileViewer
from olympia.files.models import File
from olympia.lib.cache import Message
from olympia.users.models import UserProfile

from .test_models import UploadTest


files_fixtures = 'src/olympia/files/fixtures/files/'
unicode_filenames = 'src/olympia/files/fixtures/files/unicode-filenames.xpi'
not_binary = 'install.js'
binary = 'dictionaries/ar.dic'


def create_directory(path):
    try:
        os.makedirs(path)
    except OSError:
        pass


class FilesBase(object):

    def login_as_admin(self):
        assert self.client.login(email='admin@mozilla.com')

    def login_as_reviewer(self):
        assert self.client.login(email='reviewer@mozilla.com')

    def setUp(self):
        super(FilesBase, self).setUp()
        self.addon = Addon.objects.get(pk=3615)
        self.dev = self.addon.authors.all()[0]
        self.regular = UserProfile.objects.get(pk=999)
        self.version = self.addon.versions.latest()
        self.file = self.version.all_files[0]

        p = [amo.PLATFORM_LINUX.id, amo.PLATFORM_WIN.id, amo.PLATFORM_MAC.id]
        self.file.update(platform=p[0])

        files = [
            (
                'dictionary-test.xpi',
                self.file),
            (
                'dictionary-test.xpi',
                File.objects.create(
                    version=self.version,
                    platform=p[1],
                    hash='abc123',
                    filename='dictionary-test.xpi')),
            (
                'dictionary-test-changed.xpi',
                File.objects.create(
                    version=self.version,
                    platform=p[2],
                    hash='abc123',
                    filename='dictionary-test.xpi'))]

        fixtures_base_path = os.path.join(settings.ROOT, files_fixtures)

        for xpi_file, file_obj in files:
            create_directory(os.path.dirname(file_obj.file_path))
            shutil.copyfile(
                os.path.join(fixtures_base_path, xpi_file),
                file_obj.file_path)

        self.files = [x[1] for x in files]

        self.login_as_reviewer()
        self.file_viewer = FileViewer(self.file)

    def tearDown(self):
        self.file_viewer.cleanup()
        super(FilesBase, self).tearDown()

    def files_redirect(self, file):
        return reverse('files.redirect', args=[self.file.pk, file])

    def files_serve(self, file):
        return reverse('files.serve', args=[self.file.pk, file])

    def test_view_access_anon(self):
        self.client.logout()
        self.check_urls(403)

    def test_view_access_reviewer(self):
        self.file_viewer.extract()
        self.check_urls(200)

    def test_view_access_developer(self):
        self.client.logout()
        assert self.client.login(email=self.dev.email)
        self.file_viewer.extract()
        self.check_urls(200)

    def test_view_access_reviewed(self):
        # This is disallowed for now, see Bug 1353788 for more details.
        self.file_viewer.extract()
        self.client.logout()

        for status in amo.UNREVIEWED_FILE_STATUSES:
            self.addon.update(status=status)
            self.check_urls(403)

        for status in amo.REVIEWED_STATUSES:
            self.addon.update(status=status)
            self.check_urls(403)

    def test_view_access_another_developer(self):
        self.client.logout()
        assert self.client.login(email=self.regular.email)
        self.file_viewer.extract()
        self.check_urls(403)

    def test_poll_extracted(self):
        self.file_viewer.extract()
        res = self.client.get(self.poll_url())
        assert res.status_code == 200
        assert json.loads(res.content)['status']

    def test_poll_not_extracted(self):
        res = self.client.get(self.poll_url())
        assert res.status_code == 200
        assert not json.loads(res.content)['status']

    def test_poll_extracted_anon(self):
        self.client.logout()
        res = self.client.get(self.poll_url())
        assert res.status_code == 403

    def test_content_headers(self):
        self.file_viewer.extract()
        res = self.client.get(self.file_url('install.js'))
        assert 'etag' in res._headers
        assert 'last-modified' in res._headers

    def test_content_headers_etag(self):
        self.file_viewer.extract()
        self.file_viewer.select('install.js')
        obj = getattr(self.file_viewer, 'left', self.file_viewer)
        etag = quote_etag(obj.selected.get('sha256'))
        res = self.client.get(self.file_url('install.js'),
                              HTTP_IF_NONE_MATCH=etag)
        assert res.status_code == 304

    def test_content_headers_if_modified(self):
        self.file_viewer.extract()
        self.file_viewer.select('install.js')
        obj = getattr(self.file_viewer, 'left', self.file_viewer)
        date = http_date(obj.selected.get('modified'))
        res = self.client.get(self.file_url('install.js'),
                              HTTP_IF_MODIFIED_SINCE=date)
        assert res.status_code == 304

    def test_file_header(self):
        self.file_viewer.extract()
        res = self.client.get(self.file_url(not_binary))
        url = res.context['file_link']['url']
        assert url == reverse('reviewers.review', args=[self.addon.slug])

    def test_content_no_file(self):
        self.file_viewer.extract()
        res = self.client.get(self.file_url())
        doc = pq(res.content)
        assert len(doc('#content')) == 0

    def test_files(self):
        self.file_viewer.extract()
        res = self.client.get(self.file_url())
        assert res.status_code == 200
        assert 'files' in res.context

    def test_files_anon(self):
        self.client.logout()
        res = self.client.get(self.file_url())
        assert res.status_code == 403

    def test_files_file(self):
        self.file_viewer.extract()
        res = self.client.get(self.file_url(not_binary))
        assert res.status_code == 200
        assert 'selected' in res.context

    def test_files_back_link(self):
        self.file_viewer.extract()
        res = self.client.get(self.file_url(not_binary))
        doc = pq(res.content)
        assert doc('#commands td')[-1].text_content() == 'Back to review'

    def test_files_back_link_anon(self):
        # This is disallowed for now, see Bug 1353788 for more details.
        self.file_viewer.extract()
        self.client.logout()
        res = self.client.get(self.file_url(not_binary))
        assert res.status_code == 403

    def test_diff_redirect(self):
        ids = self.files[0].id, self.files[1].id

        res = self.client.post(self.file_url(),
                               {'left': ids[0], 'right': ids[1]})
        self.assert3xx(res, reverse('files.compare', args=ids))

    def test_browse_redirect(self):
        ids = self.files[0].id,

        res = self.client.post(self.file_url(), {'left': ids[0]})
        self.assert3xx(res, reverse('files.list', args=ids))

    def test_browse_404(self):
        res = self.client.get('/files/browse/file/dont/exist.png', follow=True)
        assert res.status_code == 404

    def test_invalid_redirect(self):
        res = self.client.post(self.file_url(), {})
        self.assert3xx(res, self.file_url())

    def test_file_chooser(self):
        res = self.client.get(self.file_url())
        doc = pq(res.content)

        left = doc('#id_left')
        assert len(left) == 1

        ver = left('optgroup')
        assert len(ver) == 1

        assert ver.attr('label') == self.version.version

        files = ver('option')
        assert len(files) == 2

    def test_file_chooser_coalescing(self):
        res = self.client.get(self.file_url())
        doc = pq(res.content)

        unreviewed_file = doc('#id_left > optgroup > option.status-unreviewed')
        public_file = doc('#id_left > optgroup > option.status-public')
        assert public_file.text() == str(self.files[0].get_platform_display())
        assert unreviewed_file.text() == (
            '%s, %s' % (self.files[1].get_platform_display(),
                        self.files[2].get_platform_display()))

        assert public_file.attr('value') == str(self.files[0].id)
        assert unreviewed_file.attr('value') == str(self.files[1].id)

    def test_file_chooser_disabled_coalescing(self):
        self.files[1].update(status=amo.STATUS_DISABLED)

        res = self.client.get(self.file_url())
        doc = pq(res.content)

        disabled_file = doc('#id_left > optgroup > option.status-disabled')
        assert disabled_file.attr('value') == str(self.files[2].id)

    def test_files_for_unlisted_addon_returns_404(self):
        """Files browsing isn't allowed for unlisted addons."""
        self.make_addon_unlisted(self.addon)
        assert self.client.get(self.file_url()).status_code == 404

    def test_files_for_unlisted_addon_with_admin(self):
        """Files browsing is allowed for unlisted addons if you're admin."""
        self.login_as_admin()
        self.make_addon_unlisted(self.addon)
        assert self.client.get(self.file_url()).status_code == 200

    def test_all_versions_shown_for_admin(self):
        self.login_as_admin()
        listed_ver = version_factory(
            addon=self.addon, channel=amo.RELEASE_CHANNEL_LISTED,
            version='4.0', created=self.days_ago(1))
        unlisted_ver = version_factory(
            addon=self.addon, channel=amo.RELEASE_CHANNEL_UNLISTED,
            version='5.0')
        assert self.addon.versions.count() == 3
        res = self.client.get(self.file_url())
        doc = pq(res.content)

        left_select = doc('#id_left')
        assert left_select('optgroup').attr('label') == self.version.version
        file_options = left_select('option.status-public')
        assert len(file_options) == 3, left_select.html()
        # Check the files in the list are the two we added and the default.
        assert file_options.eq(0).attr('value') == str(
            unlisted_ver.all_files[0].pk)
        assert file_options.eq(1).attr('value') == str(
            listed_ver.all_files[0].pk)
        assert file_options.eq(2).attr('value') == str(self.file.pk)
        # Check there are prefixes on the labels for the channels
        assert file_options.eq(0).text().endswith('[Self]')
        assert file_options.eq(1).text().endswith('[AMO]')
        assert file_options.eq(2).text().endswith('[AMO]')

    def test_channel_prefix_not_shown_when_no_mixed_channels(self):
        self.login_as_admin()
        version_factory(addon=self.addon, channel=amo.RELEASE_CHANNEL_LISTED)
        assert self.addon.versions.count() == 2
        res = self.client.get(self.file_url())
        doc = pq(res.content)

        left_select = doc('#id_left')
        assert left_select('optgroup').attr('label') == self.version.version
        # Check there are NO prefixes on the labels for the channels
        file_options = left_select('option.status-public')
        assert not file_options.eq(0).text().endswith('[Self]')
        assert not file_options.eq(1).text().endswith('[AMO]')
        assert not file_options.eq(2).text().endswith('[AMO]')

    def test_only_listed_versions_shown_for_reviewer(self):
        listed_ver = version_factory(
            addon=self.addon, channel=amo.RELEASE_CHANNEL_LISTED,
            version='4.0')
        version_factory(
            addon=self.addon, channel=amo.RELEASE_CHANNEL_UNLISTED,
            version='5.0')
        assert self.addon.versions.count() == 3
        res = self.client.get(self.file_url())
        doc = pq(res.content)

        left_select = doc('#id_left')
        assert left_select('optgroup').attr('label') == self.version.version
        # Check the files in the list are just the listed, and the default.
        file_options = left_select('option.status-public')
        assert len(file_options) == 2, left_select.html()
        assert file_options.eq(0).attr('value') == str(
            listed_ver.all_files[0].pk)
        assert file_options.eq(1).attr('value') == str(self.file.pk)
        # Check there are NO prefixes on the labels for the channels
        assert not file_options.eq(0).text().endswith('[AMO]')
        assert not file_options.eq(1).text().endswith('[AMO]')


class TestFileViewer(FilesBase, TestCase):
    fixtures = ['base/addon_3615', 'base/users']

    def poll_url(self):
        return reverse('files.poll', args=[self.file.pk])

    def file_url(self, file=None):
        args = [self.file.pk]
        if file:
            args.extend(['file', file])
        return reverse('files.list', args=args)

    def check_urls(self, status):
        for url in [self.poll_url(), self.file_url()]:
            assert self.client.get(url).status_code == status

    def add_file(self, name, contents):
        dest = os.path.join(self.file_viewer.dest, name)
        open(dest, 'w').write(contents)

    def test_files_xss(self):
        self.file_viewer.extract()
        self.add_file('<script>alert("foo")', '')
        res = self.client.get(self.file_url())
        doc = pq(res.content)
        # Note: this is text, not a DOM element, so escaped correctly.
        assert '<script>alert("' in doc('#files li a').text()

    def test_content_file(self):
        self.file_viewer.extract()
        res = self.client.get(self.file_url('install.js'))
        doc = pq(res.content)
        assert len(doc('#content')) == 1

    def test_content_no_file(self):
        self.file_viewer.extract()
        res = self.client.get(self.file_url())
        doc = pq(res.content)
        assert len(doc('#content')) == 1
        assert res.context['key'] == 'install.rdf'

    def test_content_xss(self):
        self.file_viewer.extract()
        for name in ['file.txt', 'file.html', 'file.htm']:
            # If you are adding files, you need to clear out the memcache
            # file listing.
            cache.delete(self.file_viewer._cache_key())
            self.add_file(name, '<script>alert("foo")</script>')
            res = self.client.get(self.file_url(name))
            doc = pq(res.content)
            # Note: this is text, not a DOM element, so escaped correctly.
            assert doc('#content').attr('data-content').startswith('<script')

    def test_binary(self):
        viewer = self.file_viewer
        viewer.extract()
        self.add_file('file.php', '<script>alert("foo")</script>')
        res = self.client.get(self.file_url('file.php'))
        assert res.status_code == 200
        assert viewer.get_files()['file.php']['sha256'] in force_text(
            res.content)

    def test_tree_no_file(self):
        self.file_viewer.extract()
        res = self.client.get(self.file_url('doesnotexist.js'))
        assert res.status_code == 404

    def test_directory(self):
        self.file_viewer.extract()
        res = self.client.get(self.file_url('doesnotexist.js'))
        assert res.status_code == 404

    def test_unicode(self):
        self.file_viewer.src = unicode_filenames
        self.file_viewer.extract()
        res = self.client.get(self.file_url(u'\u1109\u1161\u11a9'))
        assert res.status_code == 200

    def test_unicode_unicode_tmp_path(self):
        with override_settings(TMP_PATH=str(settings.TMP_PATH)):
            self.test_unicode()

    def test_serve_no_token(self):
        self.file_viewer.extract()
        res = self.client.get(self.files_serve(binary))
        assert res.status_code == 403

    def test_serve_fake_token(self):
        self.file_viewer.extract()
        res = self.client.get(self.files_serve(binary) + '?token=aasd')
        assert res.status_code == 403

    def test_serve_bad_token(self):
        self.file_viewer.extract()
        res = self.client.get(self.files_serve(binary) + '?token=a asd')
        assert res.status_code == 403

    def test_serve_get_token(self):
        self.file_viewer.extract()
        res = self.client.get(self.files_redirect(binary))
        assert res.status_code == 302
        url = res['Location']
        assert urlparse(url).query.startswith('token=')

    def test_serve_basic(self):
        self.file_viewer.extract()
        res = self.client.get(self.files_redirect(binary))
        assert res.status_code == 302
        url = res['Location']
        token = urlparse(url).query.split('=')[1]

        res = self.client.get(self.files_serve(binary) + '?token=' + token)
        assert res.status_code == 200
        assert res['Content-Type'] == 'application/octet-stream'

        # Not sending through nginx but directly using djangos FileResponse
        # to be able to restrict and test CSP related settings.
        assert 'X-Sendfile' not in res
        assert (
            res['Content-Disposition'] ==
            'attachment; filename="ar.dic"')

        content = res.getvalue().decode('utf-8')
        assert content.startswith(
            '52726\n####الأدوات(واستثناءات)+الأفعال+الأسماء')

    @override_settings(CSP_REPORT_ONLY=False)
    def test_serve_respects_csp(self):
        self.file_viewer.extract()
        res = self.client.get(self.files_redirect(binary))
        assert res.status_code == 302
        url = res['Location']
        token = urlparse(url).query.split('=')[1]

        res = self.client.get(self.files_serve(binary) + '?token=' + token)
        assert res.status_code == 200
        assert res['Content-Type'] == 'application/octet-stream'

        # Make sure a default-src is set.
        assert "default-src 'none'" in res['content-security-policy']
        # Make sure things are as locked down as possible,
        # as per https://bugzilla.mozilla.org/show_bug.cgi?id=1566954
        assert "object-src 'none'" in res['content-security-policy']
        assert "base-uri 'none'" in res['content-security-policy']
        assert "form-action 'none'" in res['content-security-policy']
        assert "frame-ancestors 'none'" in res['content-security-policy']

        # The report-uri should be set.
        assert "report-uri" in res['content-security-policy']

        # Other properties that we defined by default aren't set
        assert "style-src" not in res['content-security-policy']
        assert "font-src" not in res['content-security-policy']
        assert "frame-src" not in res['content-security-policy']
        assert "child-src" not in res['content-security-policy']

    def test_serve_obj_not_exist(self):
        self.file_viewer.extract()
        res = self.client.get(self.files_redirect('doesnotexist.js'))
        assert res.status_code == 302
        url = res['Location']
        token = urlparse(url).query.split('=')[1]

        res = self.client.get(
            self.files_serve('doesnotexist.js') + '?token=' + token)
        assert res.status_code == 404

    def test_memcache_goes_bye_bye(self):
        self.file_viewer.extract()
        res = self.client.get(self.files_redirect(binary))
        url = res['Location']
        res = self.client.get(url)
        assert res.status_code == 200

        token = urlparse(url).query[6:]
        cache.delete('token:{}'.format(token))

        res = self.client.get(url)
        assert res.status_code == 403

    @patch.object(settings, 'FILE_VIEWER_SIZE_LIMIT', 5)
    def test_file_size(self):
        self.file_viewer.extract()
        res = self.client.get(self.file_url(not_binary))
        doc = pq(res.content)
        assert doc('.error').text().startswith('File size is')

    def test_poll_failed(self):
        msg = Message('file-viewer:%s' % self.file_viewer)
        msg.save('I like cheese.')
        res = self.client.get(self.poll_url())
        assert res.status_code == 200
        data = json.loads(res.content)
        assert not data['status']
        assert data['msg'] == ['I like cheese.']

    def test_file_chooser_selection(self):
        res = self.client.get(self.file_url())
        doc = pq(res.content)

        assert doc('#id_left option[selected]').attr('value') == (
            str(self.files[0].id))
        assert len(doc('#id_right option[value][selected]')) == 0

    def test_file_chooser_non_ascii_platform(self):
        PLATFORM_NAME = u'所有移动平台'
        f = self.files[0]
        with patch.object(File, 'get_platform_display',
                          lambda self: PLATFORM_NAME):
            assert f.get_platform_display() == PLATFORM_NAME

            res = self.client.get(self.file_url())
            doc = pq(res.content.decode('utf-8'))

            assert doc('#id_left option[value="%d"]' % f.id).text() == (
                PLATFORM_NAME)

    def test_content_file_size_uses_binary_prefix(self):
        self.file_viewer.extract()
        response = self.client.get(self.file_url('dictionaries/license.txt'))
        assert b'17.6 KiB' in response.content


class TestDiffViewer(FilesBase, TestCase):
    fixtures = ['base/addon_3615', 'base/users']

    def setUp(self):
        super(TestDiffViewer, self).setUp()
        self.file_viewer = DiffHelper(self.files[0], self.files[1])

    def poll_url(self):
        return reverse('files.compare.poll', args=[self.files[0].pk,
                                                   self.files[1].pk])

    def add_file(self, file_obj, name, contents):
        dest = os.path.join(file_obj.dest, name)
        with open(dest, 'w') as f:
            f.write(contents)

    def file_url(self, file=None):
        args = [self.files[0].pk, self.files[1].pk]
        if file:
            args.extend(['file', file])
        return reverse('files.compare', args=args)

    def check_urls(self, status):
        for url in [self.poll_url(), self.file_url()]:
            assert self.client.get(url).status_code == status

    def test_tree_no_file(self):
        self.file_viewer.extract()
        res = self.client.get(self.file_url('doesnotexist.js'))
        assert res.status_code == 404

    def test_content_file(self):
        self.file_viewer.extract()
        res = self.client.get(self.file_url(not_binary))
        doc = pq(res.content)
        assert len(doc('#content')) == 0
        assert len(doc('#diff[data-left][data-right]')) == 1

    def test_binary_serve_links(self):
        self.file_viewer.extract()
        res = self.client.get(self.file_url(binary))
        doc = pq(res.content)
        node = doc('#content-wrapper a')
        assert len(node) == 2
        assert node[0].text.startswith('Download ar.dic')

    def test_view_both_present(self):
        self.file_viewer.extract()
        res = self.client.get(self.file_url(not_binary))
        doc = pq(res.content)
        assert len(doc('#content')) == 0
        assert len(doc('#diff[data-left][data-right]')) == 1
        assert len(doc('#content-wrapper p')) == 2

    def test_view_one_missing(self):
        self.file_viewer.extract()
        os.remove(os.path.join(self.file_viewer.right.dest, 'install.js'))
        res = self.client.get(self.file_url(not_binary))
        doc = pq(res.content)
        assert len(doc('#content')) == 0
        assert len(doc('#diff[data-left][data-right]')) == 1
        assert len(doc('#content-wrapper p')) == 1

    def test_view_left_binary(self):
        self.file_viewer.extract()
        filename = os.path.join(self.file_viewer.left.dest, 'install.js')
        open(filename, 'w').write('MZ')
        res = self.client.get(self.file_url(not_binary))
        assert b'This file is not viewable online' in res.content

    def test_view_right_binary(self):
        self.file_viewer.extract()
        filename = os.path.join(self.file_viewer.right.dest, 'install.js')
        open(filename, 'w').write('MZ')
        assert not self.file_viewer.is_diffable()
        res = self.client.get(self.file_url(not_binary))
        assert b'This file is not viewable online' in res.content

    def test_different_tree(self):
        self.file_viewer.extract()
        os.remove(os.path.join(self.file_viewer.left.dest, not_binary))
        res = self.client.get(self.file_url(not_binary))
        doc = pq(res.content)
        assert doc('h4:last').text() == 'Deleted files:'
        assert len(doc('ul.root')) == 2

    def test_file_chooser_selection(self):
        res = self.client.get(self.file_url())
        doc = pq(res.content)

        assert doc('#id_left option[selected]').attr('value') == (
            str(self.files[0].id))
        assert doc('#id_right option[selected]').attr('value') == (
            str(self.files[1].id))

    def test_file_chooser_selection_same_hash(self):
        """
        In cases where multiple files are coalesced, the file selector may not
        have an actual entry for certain files. Instead, the entry with the
        identical hash should be selected.
        """
        res = self.client.get(reverse('files.compare',
                                      args=(self.files[0].id,
                                            self.files[2].id)))
        doc = pq(res.content)

        assert doc('#id_left option[selected]').attr('value') == (
            str(self.files[0].id))
        assert doc('#id_right option[selected]').attr('value') == (
            str(self.files[1].id))

    def test_files_list_uses_correct_links(self):
        res = self.client.get(reverse('files.compare',
                                      args=(self.files[0].id,
                                            self.files[2].id)))
        doc = pq(res.content)

        install_js_link = doc(
            '#files-tree li a.file[data-short="install.js"]'
        )[0].get('href')

        expected = reverse(
            'files.compare',
            args=(self.files[0].id, self.files[2].id, 'file', 'install.js'))

        assert install_js_link == expected


class TestServeFileUpload(UploadTest, TestCase):
    def setUp(self):
        super(TestServeFileUpload, self).setUp()

        self.upload = self.get_upload('webextension.xpi')
        self.url = reverse('files.serve_file_upload',
                           args=[self.upload.uuid.hex])

    def test_returns_error_when_no_access_token(self):
        resp = self.client.get(self.url)

        assert resp.status_code == 403

    def test_returns_error_when_access_token_is_invalid(self):
        resp = self.client.get('{}?access_token=nope'.format(self.url))

        assert resp.status_code == 403

    def test_get(self):
        resp = self.client.get(self.upload.get_authenticated_download_url())

        assert resp.status_code == 200
        assert resp['content-type'] == 'application/octet-stream'
        assert resp[settings.XSENDFILE_HEADER] == self.upload.path

    def test_returns_410_when_upload_path_is_falsey(self):
        self.upload.path = ''
        self.upload.save()

        resp = self.client.get(self.upload.get_authenticated_download_url())

        assert resp.status_code == 410


class TestBrowseRedirect(TestCase):
    def setUp(self):
        super(TestBrowseRedirect, self).setUp()

        self.version = version_factory(addon=addon_factory())
        self.browse_redirect_url = reverse(
            'files.browse_redirect', args=[self.version.pk]
        )

    def test_redirects_to_file_viewer_with_file_id(self):
        response = self.client.get(self.browse_redirect_url)

        self.assert3xx(
            response,
            reverse('files.list', args=[self.version.current_file.id]),
        )

    def test_redirects_to_file_viewer_with_file_query_param(self):
        file = 'some/file.js'
        response = self.client.get(self.browse_redirect_url + f'?file={file}')

        self.assert3xx(
            response,
            reverse('files.list', args=[
                self.version.current_file.id,
                'file',
                file,
            ]),
        )

    def test_returns_404_when_version_is_not_found(self):
        browse_redirect_url = reverse('files.browse_redirect', args=[123456])

        response = self.client.get(browse_redirect_url)

        assert response.status_code == 404


class TestCompareRedirect(TestCase):
    def setUp(self):
        super(TestCompareRedirect, self).setUp()

        addon = addon_factory()
        self.base_version = version_factory(addon=addon)
        self.head_version = version_factory(addon=addon)
        self.compare_redirect_url = reverse(
            'files.compare_redirect',
            args=[self.base_version.pk, self.head_version.pk],
        )

    def test_redirects_to_file_viewer_with_file_ids(self):
        response = self.client.get(self.compare_redirect_url)

        self.assert3xx(
            response,
            reverse('files.compare', args=[
                self.base_version.current_file.id,
                self.head_version.current_file.id,
            ]),
        )

    def test_redirects_to_file_viewer_with_file_query_param(self):
        file = 'some/file.js'
        response = self.client.get(self.compare_redirect_url + f'?file={file}')

        self.assert3xx(
            response,
            reverse('files.compare', args=[
                self.base_version.current_file.id,
                self.head_version.current_file.id,
                'file',
                file,
            ]),
        )

    def test_returns_404_when_head_version_is_not_found(self):
        compare_redirect_url = reverse('files.compare_redirect', args=[
            self.base_version.pk,
            123456,
        ])

        response = self.client.get(compare_redirect_url)

        assert response.status_code == 404

    def test_returns_404_when_base_version_is_not_found(self):
        compare_redirect_url = reverse('files.compare_redirect', args=[
            123456,
            self.base_version.pk,
        ])

        response = self.client.get(compare_redirect_url)

        assert response.status_code == 404
