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
