from argon2 import PasswordHasher

from hisaab.auth.passwords import hash_password, verify_password


def test_hash_is_argon2id():
    assert hash_password("correct horse").startswith("$argon2id$")


def test_same_password_hashes_differently_each_time():
    assert hash_password("same") != hash_password("same")  # per-hash random salt


def test_verify_accepts_right_password():
    ok, _ = verify_password(hash_password("right one"), "right one")
    assert ok


def test_verify_rejects_wrong_password():
    ok, needs_rehash = verify_password(hash_password("right one"), "wrong one")
    assert (ok, needs_rehash) == (False, False)


def test_verify_rejects_garbage_hash_instead_of_raising():
    assert verify_password("not-a-hash", "anything") == (False, False)


def test_weaker_parameters_flag_a_rehash():
    weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash("pw")
    assert verify_password(weak, "pw") == (True, True)
