import pytest
import rsa


@pytest.fixture(scope='session')
def test_rsa_key():
    """A throwaway 2048-bit ``(rsa.PublicKey, rsa.PrivateKey)`` pair."""
    try:
        from cryptography.hazmat.primitives.asymmetric import rsa as crypto_rsa
    except ImportError:
        return rsa.newkeys(2048, accurate=False)  # pure Python, a few seconds

    numbers = crypto_rsa.generate_private_key(65537, 2048).private_numbers()
    n, e = numbers.public_numbers.n, numbers.public_numbers.e
    return rsa.PublicKey(n, e), rsa.PrivateKey(n, e, numbers.d, numbers.p, numbers.q)


@pytest.fixture
def server_rsa_key(test_rsa_key, monkeypatch):
    """Registers `test_rsa_key` as a server key; returns ``(fingerprint, private key)``."""
    from telethon.crypto import rsa as tl_rsa
    public, private = test_rsa_key
    monkeypatch.setattr(tl_rsa, '_server_keys', dict(tl_rsa._server_keys))
    tl_rsa.add_key(public.save_pkcs1().decode('ascii'), old=False)
    return tl_rsa._compute_fingerprint(public), private
