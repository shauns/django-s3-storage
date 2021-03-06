# coding=utf-8
from __future__ import unicode_literals
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest import skipIf
import os
import requests
import django
from django.core.exceptions import ImproperlyConfigured
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management import call_command, CommandError
from django.contrib.staticfiles.storage import staticfiles_storage
from django.test import SimpleTestCase
from django.utils.six import StringIO
from django.utils.six.moves.urllib.parse import urlsplit, urlunsplit
from django_s3_storage.storage import S3Storage, StaticS3Storage


class TestS3Storage(SimpleTestCase):

    # Helpers.

    @contextmanager
    def save_file(self, name="foo.txt", content=b"foo", storage=default_storage):
        name = storage.save(name, ContentFile(content, name))
        try:
            yield name
        finally:
            storage.delete(name)

    # Configuration tets.

    def testSettingsImported(self):
        self.assertEqual(S3Storage().settings.AWS_S3_CONTENT_LANGUAGE, "")
        with self.settings(AWS_S3_CONTENT_LANGUAGE="foo"):
            self.assertEqual(S3Storage().settings.AWS_S3_CONTENT_LANGUAGE, "foo")

    def testSettingsOverwritenBySuffixedSettings(self):
        self.assertEqual(StaticS3Storage().settings.AWS_S3_CONTENT_LANGUAGE, "")
        with self.settings(AWS_S3_CONTENT_LANGUAGE="foo", AWS_S3_CONTENT_LANGUAGE_STATIC="bar"):
            self.assertEqual(StaticS3Storage().settings.AWS_S3_CONTENT_LANGUAGE, "bar")

    def testSettingsOverwrittenByKwargs(self):
        self.assertEqual(S3Storage().settings.AWS_S3_CONTENT_LANGUAGE, "")
        self.assertEqual(S3Storage(aws_s3_content_language="foo").settings.AWS_S3_CONTENT_LANGUAGE, "foo")

    def testSettingsCannotUsePublicUrlAndBucketAuth(self):
        self.assertRaises(ImproperlyConfigured, lambda: S3Storage(
            aws_s3_bucket_auth=True,
            aws_s3_public_url="/foo/",
        ))

    def testSettingsUnknown(self):
        self.assertRaises(ImproperlyConfigured, lambda: S3Storage(
            foo=True,
        ))

    # Storage tests.

    @skipIf(django.VERSION < (1, 10), "Feature not supported by Django")
    def testGenerateFilename(self):
        self.assertEqual(default_storage.generate_filename(os.path.join("foo", ".", "bar.txt")), "foo/bar.txt")

    def testOpenMissing(self):
        self.assertRaises(IOError, lambda: default_storage.open("foo.txt"))

    def testOpenWriteMode(self):
        self.assertRaises(ValueError, lambda: default_storage.open("foo.txt", "wb"))

    def testSaveAndOpen(self):
        with self.save_file() as name:
            self.assertEqual(name, "foo.txt")
            handle = default_storage.open(name)
            self.assertEqual(handle.read(), b"foo")
            # Re-open the file.
            handle.close()
            handle.open()
            self.assertEqual(handle.read(), b"foo")

    def testSaveTextMode(self):
        with self.save_file(content="foo"):
            self.assertEqual(default_storage.open("foo.txt").read(), b"foo")

    def testSaveGzipped(self):
        # Tiny files are not gzipped.
        with self.save_file():
            self.assertEqual(default_storage.meta("foo.txt").get("ContentEncoding"), None)
            self.assertEqual(default_storage.open("foo.txt").read(), b"foo")
            self.assertEqual(requests.get(default_storage.url("foo.txt")).content, b"foo")
        # Large files are gzipped.
        with self.save_file(content=b"foo" * 1000):
            self.assertEqual(default_storage.meta("foo.txt").get("ContentEncoding"), "gzip")
            self.assertEqual(default_storage.open("foo.txt").read(), b"foo" * 1000)
            self.assertEqual(requests.get(default_storage.url("foo.txt")).content, b"foo" * 1000)

    def testUrl(self):
        with self.save_file():
            url = default_storage.url("foo.txt")
            # The URL should contain query string authentication.
            self.assertTrue(urlsplit(url).query)
            response = requests.get(url)
            # The URL should be accessible, but be marked as private.
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, b"foo")
            self.assertEqual(response.headers["cache-control"], "private,max-age=3600")
            # With the query string removed, the URL should not be accessible.
            url_unauthenticated = urlunsplit(urlsplit(url)[:3] + ("", "",))
            response_unauthenticated = requests.get(url_unauthenticated)
            self.assertEqual(response_unauthenticated.status_code, 403)

    def testExists(self):
        self.assertFalse(default_storage.exists("foo.txt"))
        with self.save_file():
            self.assertTrue(default_storage.exists("foo.txt"))

    def testSize(self):
        with self.save_file():
            self.assertEqual(default_storage.size("foo.txt"), 3)

    def testDelete(self):
        with self.save_file():
            self.assertTrue(default_storage.exists("foo.txt"))
            default_storage.delete("foo.txt")
        self.assertFalse(default_storage.exists("foo.txt"))

    def testModifiedTime(self):
        with self.save_file():
            modified_time = default_storage.modified_time("foo.txt")
            # Check that the timestamps are roughly equals.
            self.assertLess(abs(modified_time - datetime.now()), timedelta(seconds=10))
            # All other timestamps are slaved to modified time.
            self.assertEqual(default_storage.accessed_time("foo.txt"), modified_time)
            self.assertEqual(default_storage.created_time("foo.txt"), modified_time)

    def testListdir(self):
        self.assertEqual(default_storage.listdir(""), ([], []))
        self.assertEqual(default_storage.listdir("/"), ([], []))
        with self.save_file(), self.save_file(name="bar/bat.txt"):
            self.assertEqual(default_storage.listdir(""), (["bar"], ["foo.txt"]))
            self.assertEqual(default_storage.listdir("/"), (["bar"], ["foo.txt"]))
            self.assertEqual(default_storage.listdir("bar"), ([], ["bat.txt"]))
            self.assertEqual(default_storage.listdir("/bar"), ([], ["bat.txt"]))
            self.assertEqual(default_storage.listdir("bar/"), ([], ["bat.txt"]))

    def testSyncMeta(self):
        with self.save_file(name="foo/bar.txt", content=b"foo" * 1000):
            meta = default_storage.meta("foo/bar.txt")
            self.assertEqual(meta["CacheControl"], "private,max-age=3600")
            self.assertEqual(meta["ContentType"], "text/plain")
            self.assertEqual(meta["ContentEncoding"], "gzip")
            self.assertEqual(meta.get("ContentDisposition"), None)
            self.assertEqual(meta.get("ContentLanguage"), None)
            self.assertEqual(meta["Metadata"], {})
            self.assertEqual(meta.get("StorageClass"), None)
            self.assertEqual(meta.get("ServerSideEncryption"), None)
            # Store new metadata.
            with self.settings(
                AWS_S3_BUCKET_AUTH=False,
                AWS_S3_MAX_AGE_SECONDS=9999,
                AWS_S3_CONTENT_DISPOSITION=lambda name: "attachment; filename={}".format(name),
                AWS_S3_CONTENT_LANGUAGE="eo",
                AWS_S3_METADATA={
                    "foo": "bar",
                    "baz": lambda name: name,
                },
                AWS_S3_REDUCED_REDUNDANCY=True,
                AWS_S3_ENCRYPT_KEY=True,
            ):
                default_storage.sync_meta()
            # Check metadata changed.
            meta = default_storage.meta("foo/bar.txt")
            self.assertEqual(meta["CacheControl"], "public,max-age=9999")
            self.assertEqual(meta["ContentType"], "text/plain")
            self.assertEqual(meta["ContentEncoding"], "gzip")
            self.assertEqual(meta.get("ContentDisposition"), "attachment; filename=foo/bar.txt")
            self.assertEqual(meta.get("ContentLanguage"), "eo")
            self.assertEqual(meta.get("Metadata"), {
                "foo": "bar",
                "baz": "foo/bar.txt",
            })
            self.assertEqual(meta["StorageClass"], "REDUCED_REDUNDANCY")
            self.assertEqual(meta["ServerSideEncryption"], "AES256")
            # Check ACL changed by removing the query string.
            url_unauthenticated = urlunsplit(urlsplit(default_storage.url("foo/bar.txt"))[:3] + ("", "",))
            response = requests.get(url_unauthenticated)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, b"foo" * 1000)

    def testPublicUrl(self):
        with self.settings(AWS_S3_PUBLIC_URL="/foo/", AWS_S3_BUCKET_AUTH=False):
            self.assertEqual(default_storage.url("bar.txt"), "/foo/bar.txt")

    def testEndpointUrl(self):
        with self.settings(AWS_S3_ENDPOINT_URL="https://s3.amazonaws.com"), self.save_file() as name:
            self.assertEqual(name, "foo.txt")
            self.assertEqual(default_storage.open(name).read(), b"foo")

    # Static storage tests.

    def testStaticSettings(self):
        self.assertEqual(staticfiles_storage.settings.AWS_S3_BUCKET_AUTH, False)
        self.assertEqual(staticfiles_storage.settings.AWS_S3_MAX_AGE_SECONDS, 31536000)

    def testStaticUrl(self):
        with self.save_file(storage=staticfiles_storage):
            url = staticfiles_storage.url("foo.txt")
            # The URL should not contain query string authentication.
            self.assertFalse(urlsplit(url).query)
            response = requests.get(url)
            # The URL should be accessible, but be marked as public.
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, b"foo")
            self.assertEqual(response.headers["cache-control"], "public,max-age=31536000")

    # Management commands.

    def testManagementS3SyncMeta(self):
        with self.save_file():
            # Store new metadata.
            with self.settings(AWS_S3_MAX_AGE_SECONDS=9999):
                call_command("s3_sync_meta", "django.core.files.storage.default_storage", stdout=StringIO())
            # Check metadata changed.
            meta = default_storage.meta("foo.txt")
            self.assertEqual(meta["CacheControl"], "private,max-age=9999")

    def testManagementS3SyncMetaUnknownStorage(self):
        self.assertRaises(CommandError, lambda: call_command("s3_sync_meta", "foo.bar", stdout=StringIO()))
