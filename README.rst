==============
Validate_email
==============

Validate_email is a package for Python that check if an email is valid, properly formatted and really exists.



INSTALLATION
============

First, you must do::

    pip install validate_email

Extra
------

For check the domain mx and verify email exits you must have the `pyDNS` package installed::

    pip install pyDNS


USAGE
=====

Basic usage::

    from validate_email import validate_email
    is_valid = validate_email('example@example.com')


Checking domain has SMTP Server
-------------------------------

Check if the host has SMTP Server::

    from validate_email import validate_email
    is_valid = validate_email('example@example.com',check_mx=True)


Set Up Debug Logging
--------------------
import logging
import sys

root = logging.getLogger("validate_email")
root.setLevel(logging.DEBUG)

ch = logging.StreamHandler(sys.stdout)
ch.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
ch.setFormatter(formatter)
root.addHandler(ch)

from validate_email import validate_email
is_valid = validate_email('wello7@yahoo.com',sending_email='hydration@yahoo.com',check_mx=True,debug=True) 
print("MX CHECK: %s") % (is_valid)
is_valid = validate_email('wello7@yahoo.com',sending_email='hydration@yahoo.com',verify=True,debug=True) 
print("VERIFY: %s") % (is_valid)


Verify email exists
-------------------

Check if the host has SMTP Server and the email really exists::

    from validate_email import validate_email
    is_valid = validate_email('example@example.com',verify=True)

Verify email exists on a server that implements callback verfication
-------------------

Check if the host has SMTP Server and the email really exists::

    from validate_email import validate_email
    is_valid = validate_email('example@example.com',verify=True,sending_email="valid@example.org")

valid@example.org must be a valid e-mail that you control.

For information on callback verification see: https://en.wikipedia.org/wiki/Callback_verification

Don't allow your users to register with disposable emails
---------------------------------------------------------

Checks are performed against this version of the listing:
https://github.com/martenson/disposable-email-domains/tree/4efb0b0ed43022a78abc768c4f28ba7ed7a37772

    from validate_email import validate_email
    is_valid = validate_email("disposable@yopmail.com", allow_disposable=False)
    assert not is_valid


TODOs and BUGS
==============
See: http://github.com/syrusakbary/validate_email/issues
