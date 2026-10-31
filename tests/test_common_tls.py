import hashlib
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "toolkit" / "scripts"))
from common import ConfigurationError, TransportError, client_for  # noqa: E402  guard-ok: scripts are imported after the sys.path insert, as in the other test modules


def write_env(directory, extra=""):
    path = Path(directory) / ".env"
    path.write_text("orch_1=x_vlab\nx_vlab_base_url=https://192.0.2.10\nx_vlab_api_key=K\n" + extra, encoding="utf-8")
    return str(path)


class TlsSettingsTests(unittest.TestCase):
    def test_profile_name_never_turns_off_certificate_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            config = client_for("x_vlab", write_env(directory)).config
        self.assertTrue(config.verify_tls)
        self.assertIsNone(config.tls_sha256)

    def test_pin_is_normalized_and_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            config = client_for("x_vlab", write_env(directory, "x_vlab_tls_sha256=" + "AB:" * 31 + "AB\n")).config
            self.assertEqual(config.tls_sha256, "ab" * 32)
            with self.assertRaisesRegex(ConfigurationError, "tls_sha256"):
                client_for("x_vlab", write_env(directory, "x_vlab_tls_sha256=abc\n"))


class UserinfoInTheUrl(unittest.TestCase):
    """A base URL with user:password@ would reach http.client, whose InvalidURL traceback prints the password."""

    def env(self, directory, url):
        path = Path(directory) / ".env"
        path.write_text("orch_1=x\nx_base_url={}\nx_api_key=K\n".format(url), encoding="utf-8")
        return str(path)

    def test_a_url_with_a_user_or_password_is_refused_without_echoing_it(self):
        for url in ("https://user:hunter2x@host.invalid", "https://user:hunter2x@host.invalid:443/gms/rest", "https://hunter2x@host.invalid", "https://[::1]:hunter2x@host.invalid"):
            with self.subTest(url=url), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(ConfigurationError) as refused:
                    client_for("x", self.env(directory, url))
                self.assertNotIn("hunter2x", str(refused.exception))
                self.assertTrue(refused.exception.__suppress_context__ or refused.exception.__context__ is None)
                # a malformed [host] is urlsplit's ValueError on newer Pythons, the userinfo refusal on 3.9: both name no secret
                self.assertRegex(str(refused.exception), "x_api_key" if "[" not in url else "x_api_key|https://")

    def test_a_url_with_a_page_path_is_refused_with_one_clear_line(self):
        """A web UI address (https://host/<release>/webui/login) answered /gms/versions with HTML: a JSONDecodeError traceback, not a message."""
        for url in ("https://host.invalid/10.0.0.40724/webui/login", "https://host.invalid/webui/", "https://host.invalid/?a=1", "https://host.invalid/#x"):
            with self.subTest(url=url), tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(ConfigurationError, r"base URL for 'x' must be the Orchestrator's address only \(https://<host>\)"):
                    client_for("x", self.env(directory, url))
        for url, kept in (("https://host.invalid", "https://host.invalid/gms/rest"), ("https://host.invalid/", "https://host.invalid/gms/rest"),
                          ("https://host.invalid:8443/gms/rest/", "https://host.invalid:8443/gms/rest"), ("https://host.invalid/proxy/gms/rest", "https://host.invalid/proxy/gms/rest")):
            with self.subTest(url=url), tempfile.TemporaryDirectory() as directory:
                self.assertEqual(client_for("x", self.env(directory, url)).config.base_url, kept)

    def test_an_http_client_error_is_a_transport_error_without_its_text(self):
        from http.client import BadStatusLine, InvalidURL

        class Opener:
            def __init__(self, error):
                self.error = error

            def open(self, req, timeout=None):
                raise self.error
        with tempfile.TemporaryDirectory() as directory:
            client = client_for("x", self.env(directory, "https://host.invalid"))
        for error in (InvalidURL("nonnumeric port: 'hunter2x@host.invalid'"), BadStatusLine("hunter2x")):
            with self.subTest(error=type(error).__name__):
                client._opener = Opener(error)
                with self.assertRaises(TransportError) as failed:
                    client.get("/gms/versions")
                self.assertNotIn("hunter2x", str(failed.exception))
                self.assertIsNone(failed.exception.__cause__)
                self.assertTrue(failed.exception.__suppress_context__)  # a printed traceback would show the hidden error's text
                self.assertIn(type(error).__name__, str(failed.exception))

    def test_a_cut_off_error_body_is_an_http_error_not_a_traceback(self):
        import io
        from http.client import IncompleteRead
        from urllib.error import HTTPError
        from common import EdgeConnectError

        class Body(io.BytesIO):
            def read(self, *a):
                raise IncompleteRead(b"abc", 10)

        class Opener:
            def open(self, req, timeout=None):
                raise HTTPError(req.full_url, 500, "Server Error", {}, Body())
        with tempfile.TemporaryDirectory() as directory:
            client = client_for("x", self.env(directory, "https://host.invalid"))
        client._opener = Opener()
        with self.assertRaises(EdgeConnectError) as failed:
            client.get("/gms/versions")
        self.assertIn("500", str(failed.exception))


class PinnedConnectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        key, cert = Path(cls.directory.name) / "key.pem", Path(cls.directory.name) / "cert.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=not-this-host",
                        "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True)
        cls.pin = hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert.read_text())).hexdigest()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok": true}')

            def log_message(self, *args):
                pass

        cls.server = HTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert), str(key))
        cls.server.socket = context.wrap_socket(cls.server.socket, server_side=True)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.directory.cleanup()

    def client(self, extra):
        env = Path(self.directory.name) / ".env"
        env.write_text("orch_1=lab\nlab_base_url=https://127.0.0.1:{}\nlab_api_key=K\n{}".format(self.server.server_port, extra), encoding="utf-8")
        return client_for("lab", str(env))

    def test_matching_pin_connects_to_a_self_signed_certificate(self):
        self.assertEqual(self.client("lab_tls_sha256={}\n".format(self.pin)).get("/x"), {"ok": True})

    def test_wrong_pin_is_refused(self):
        with self.assertRaisesRegex(TransportError, "pin"):
            self.client("lab_tls_sha256={}\n".format("0" * 64)).get("/x")

    def test_without_pin_a_self_signed_certificate_is_refused(self):
        with self.assertRaises(TransportError):
            self.client("").get("/x")


if __name__ == "__main__":
    unittest.main()
