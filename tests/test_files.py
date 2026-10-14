import asyncio
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import google_chat


class FilesPolicyCase(unittest.TestCase):
    """A fresh files policy over a temporary directory for each test."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        policy = mock.patch.dict(google_chat.files_policy, {'dir': None, 'any_path': False, 'also': []})
        policy.start()
        self.addCleanup(policy.stop)

    def configure(self, files_dir=None, any_path=False, environ=None):
        return google_chat.configure_files(files_dir, str(self.tmp / 'token.json'), any_path, environ or {})


class ConfigureFilesTest(FilesPolicyCase):
    def test_the_default_is_files_beside_the_token_created_private(self):
        chosen = self.configure()
        self.assertEqual(chosen, self.tmp / 'files')
        self.assertEqual(chosen.stat().st_mode & 0o777, 0o700)
        self.assertEqual(google_chat.files_dir(), chosen)

    def test_the_gateway_names_the_directory(self):
        chosen = self.configure(environ={google_chat.FILES_DIR_ENV: str(self.tmp / 'gw')})
        self.assertEqual(chosen, self.tmp / 'gw')

    def test_a_flag_that_agrees_with_the_gateway_is_fine(self):
        same = str(self.tmp / 'gw')
        self.assertEqual(self.configure(same, environ={google_chat.FILES_DIR_ENV: same}), self.tmp / 'gw')

    def test_a_flag_that_disagrees_with_the_gateway_fails_the_start(self):
        with self.assertRaisesRegex(ValueError, 'the directory the gateway'):
            self.configure(str(self.tmp / 'mine'), environ={google_chat.FILES_DIR_ENV: str(self.tmp / 'gw')})

    def test_using_it_unconfigured_fails_loud(self):
        with self.assertRaises(RuntimeError):
            google_chat.files_dir()


class AttachmentSourceTest(FilesPolicyCase):
    def setUp(self):
        super().setUp()
        self.files = self.configure()
        (self.files / 'uploads' / 'id').mkdir(parents=True)
        self.upload = self.files / 'uploads' / 'id' / 'report.pdf'
        self.upload.write_bytes(b'%PDF')
        self.secret = self.tmp / 'token.json'
        self.secret.write_text('{"refresh_token": "x"}')

    def test_a_file_under_the_directory_is_attached(self):
        real = os.path.realpath(self.upload)
        self.assertEqual(google_chat.attachment_source(str(self.upload)), (real, None))
        self.assertEqual(google_chat.attachment_source('uploads/id/report.pdf'), (real, None))

    def test_a_file_anywhere_else_is_refused(self):
        (self.files / 'escape').symlink_to(self.secret)
        for entry in [str(self.secret), '../token.json', 'escape', str(self.files), '.', '/etc/hosts']:
            with self.assertRaisesRegex(Exception, 'Only a file under'):
                google_chat.attachment_source(entry)

    def test_a_url_is_not_fetched(self):
        with mock.patch.object(google_chat.urllib.request, 'urlretrieve') as fetch:
            for entry in ['https://example.com/a.png', 'http://github.int.exe.xyz/user', 'file:///etc/hosts']:
                with self.assertRaisesRegex(Exception, 'A URL is not fetched'):
                    google_chat.attachment_source(entry)
        fetch.assert_not_called()

    def test_a_missing_file_or_a_directory_is_not_found(self):
        for entry in ['missing.txt', 'uploads/id']:
            with self.assertRaisesRegex(Exception, 'File not found'):
                google_chat.attachment_source(entry)

    def test_a_channel_delivery_can_be_forwarded(self):
        # attachments/ beside the token, the channel's default, and any other the channel names.
        for delivered in [self.tmp / 'attachments', self.tmp / 'state' / 'attachments']:
            delivered.mkdir(parents=True)
            (delivered / 'gchat-1.png').write_bytes(b'png')
        google_chat.attach_also_from(self.tmp / 'state' / 'attachments')
        for delivered in [self.tmp / 'attachments', self.tmp / 'state' / 'attachments']:
            real = os.path.realpath(delivered / 'gchat-1.png')
            self.assertEqual(google_chat.attachment_source(str(delivered / 'gchat-1.png')), (real, None))
            with self.assertRaisesRegex(Exception, 'Only a file under'):
                google_chat.attachment_source(str(delivered))

    def test_allow_any_path_restores_paths_and_urls(self):
        google_chat.files_policy['any_path'] = True
        self.assertEqual(google_chat.attachment_source(str(self.secret)), (str(self.secret), None))
        with self.assertRaisesRegex(Exception, 'File not found'):
            google_chat.attachment_source(str(self.tmp / 'missing'))

        def fetch(url, path):
            Path(path).write_bytes(b'img')
        with mock.patch.object(google_chat.urllib.request, 'urlretrieve', side_effect=fetch):
            local, temp = google_chat.attachment_source('https://example.com/a/pic.png')
        self.assertEqual(local, temp)
        self.assertTrue(local.endswith('.png'))
        self.assertEqual(Path(local).read_bytes(), b'img')
        os.unlink(local)

    def test_a_failed_fetch_leaves_no_temporary_file(self):
        google_chat.files_policy['any_path'] = True
        made = []
        real_named = tempfile.NamedTemporaryFile

        def named(**kwargs):
            handle = real_named(**kwargs)
            made.append(handle.name)
            return handle
        with mock.patch('tempfile.NamedTemporaryFile', side_effect=named), \
                mock.patch.object(google_chat.urllib.request, 'urlretrieve', side_effect=OSError('down')):
            with self.assertRaises(OSError):
                google_chat.attachment_source('https://example.com/a.png')
        self.assertEqual(len(made), 1)
        self.assertFalse(os.path.exists(made[0]))


class SendRefusesBeforeUploadingTest(FilesPolicyCase):
    def setUp(self):
        super().setUp()
        self.configure()
        self.chat = mock.MagicMock()
        for target, kw in [('get_credentials', {'return_value': object()}),
                           ('_get_service', {'return_value': self.chat})]:
            p = mock.patch.object(google_chat, target, **kw)
            p.start()
            self.addCleanup(p.stop)

    def test_send_and_update_refuse_a_path_outside(self):
        (self.tmp / 'token.json').write_text('{}')
        with self.assertRaisesRegex(Exception, 'Only a file under'):
            asyncio.run(google_chat.send_space_message('spaces/S', 'hi', file_paths=[str(self.tmp / 'token.json')]))
        with self.assertRaisesRegex(Exception, 'A URL is not fetched'):
            asyncio.run(google_chat.update_message('spaces/S/messages/M', file_paths=['http://169.254.169.254/']))
        self.chat.media().upload.assert_not_called()
        self.chat.spaces().messages().create.assert_not_called()
        self.chat.spaces().messages().patch.assert_not_called()


class DownloadAttachmentTest(FilesPolicyCase):
    def setUp(self):
        super().setUp()
        self.files = self.configure()
        p = mock.patch.object(google_chat, 'get_credentials', return_value=object())
        p.start()
        self.addCleanup(p.stop)

    def download(self, content_name, content_type='application/octet-stream'):
        resp = mock.Mock(ok=True, headers={'Content-Type': content_type})
        resp.iter_content.return_value = [b'data']
        session = mock.Mock()
        session.get.return_value = resp
        with mock.patch.object(google_chat, '_http', return_value=session):
            return asyncio.run(google_chat.download_attachment('RES', content_name))

    def test_an_attachment_is_saved_into_the_files_directory(self):
        saved = self.download('report.pdf', 'application/pdf')
        path = Path(saved['path'])
        self.assertEqual(path.parent, self.files)
        self.assertEqual(path.suffix, '.pdf')
        self.assertEqual(path.read_bytes(), b'data')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_the_senders_name_chooses_only_a_plain_extension(self):
        self.assertTrue(self.download('evil.pth')['path'].endswith('.pth'))
        for name in ['a.p/../../x', 'a.‮gpj', 'a.' + 'x' * 11, 'noext']:
            self.assertTrue(self.download(name)['path'].endswith('.bin'), name)

    def test_a_generic_type_from_google_takes_the_type_the_name_implies(self):
        # Google's media endpoint answered application/octet-stream for a PNG.
        for header in ['application/octet-stream', 'application/octet-stream; charset=binary']:
            saved = self.download('Screenshot.png', header)
            self.assertEqual(saved['contentType'], 'image/png', header)
            self.assertTrue(saved['path'].endswith('.png'), header)
        self.assertEqual(self.download('doc.pdf')['contentType'], 'application/pdf')

    def test_a_name_with_no_known_type_keeps_the_generic_one(self):
        for name in ['noext', 'evil.pth', None]:
            self.assertEqual(self.download(name)['contentType'], 'application/octet-stream', name)

    def test_a_specific_type_from_google_wins_over_the_name(self):
        saved = self.download('photo.png', 'application/pdf')
        self.assertEqual(saved['contentType'], 'application/pdf')
        self.assertTrue(saved['path'].endswith('.pdf'))


if __name__ == '__main__':
    unittest.main()
