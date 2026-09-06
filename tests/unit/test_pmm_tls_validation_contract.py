#!/usr/bin/env python3
"""PMM probe trust must reject untrusted peers before application data."""

import importlib.util
import socket
import ssl
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

REPO = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("pmm_probe_common", REPO / "tests/lab/_probe_common.py")
COMMON = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(COMMON)


class PmmTlsValidationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        root = Path(cls.directory.name)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
        now = datetime.now(timezone.utc)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(
                digital_signature=True, content_commitment=False, key_encipherment=True,
                data_encipherment=False, key_agreement=False, key_cert_sign=True,
                crl_sign=True, encipher_only=False, decipher_only=False,
            ), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256())
        )
        cls.ca = root / "certificate.pem"
        cls.ca.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        private_key = root / "key.pem"
        private_key.write_bytes(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ))
        private_key.chmod(0o600)
        cls.server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cls.server_context.load_cert_chain(cls.ca, private_key)

    def exchange(self, config, hostname="localhost"):
        """A finite socketpair handshake; no listener or live infrastructure."""
        client, server = socket.socketpair()
        client.settimeout(5)
        server.settimeout(5)

        def serve():
            with server:
                try:
                    with self.server_context.wrap_socket(server, server_side=True) as tls:
                        data = tls.recv(1)
                        tls.sendall(data)
                except ssl.SSLError:
                    # The client deliberately aborts negative-control handshakes.
                    pass

        with ThreadPoolExecutor(max_workers=1) as executor:
            worker = executor.submit(serve)
            try:
                with client:
                    context = COMMON.pmm_ssl_context(config)
                    with context.wrap_socket(client, server_hostname=hostname) as tls:
                        tls.sendall(b"x")
                        return tls.recv(1)
            finally:
                client.close()
                worker.result(timeout=6)

    def test_declared_ca_allows_authenticated_exchange(self):
        self.assertEqual(self.exchange({"validate_certs": True, "ca_reference": str(self.ca)}), b"x")

    def test_environment_cannot_disable_untrusted_peer_rejection(self):
        with patch.dict("os.environ", {"PMM_VALIDATE_CERTS": "0"}):
            with self.assertRaises(ssl.SSLCertVerificationError):
                self.exchange({"validate_certs": True})

    def test_trusted_ca_does_not_excuse_wrong_server_identity(self):
        with self.assertRaises(ssl.SSLCertVerificationError):
            self.exchange({"validate_certs": True, "ca_reference": str(self.ca)}, "other.invalid")


if __name__ == "__main__":
    unittest.main()
