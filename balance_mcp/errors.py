"""Refusals the assistant should read, as opposed to bugs."""


class BalanceMcpError(Exception):
    """A write or a query the server will not carry out.

    The message is the whole of what the assistant is told. It names what to
    change and it does not include a traceback.
    """
