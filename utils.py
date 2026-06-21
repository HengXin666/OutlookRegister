import random
import string
import secrets


def random_email(length=None, prefix=""):
    if length is None:
        length = random.randint(12, 14)

    if prefix and not prefix.isalnum():
        raise ValueError("email_prefix 只能包含字母和数字")

    first_char = random.choice(string.ascii_lowercase)

    other_chars = []
    for _ in range(length - 1):  
        if random.random() < 0.07:  
            other_chars.append(random.choice(string.digits))
        else: 
            other_chars.append(random.choice(string.ascii_lowercase))

    email = first_char + ''.join(other_chars)
    if not prefix:
        return email

    return prefix + email[len(prefix):]


def generate_strong_password(length=None):
    if length is None:
        length = random.randint(11, 15)

    chars = string.ascii_letters + string.digits + "!@#$%^&*"

    while True:
        password = ''.join(secrets.choice(chars) for _ in range(length))

        if (any(c.islower() for c in password) 
                and any(c.isupper() for c in password)
                and any(c.isdigit() for c in password)
                and any(c in "!@#$%^&*" for c in password)):
            return password
