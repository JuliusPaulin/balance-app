"""What the app knows how to work out, kept away from the routes that ask.

``networth``           carries the last balance of each account forward, and
                       groups the accounts and their holdings.
``recurring``          finds the repeating charges in a transaction history
                       and says which kind of repeat each one is.
``investment_import``  parses Nordnet CSV and Nordea xlsx portfolio exports.
``enable_banking``     the Open Banking client: consent, sessions, fetch.
``planning``           long-term cash flow: the projection, the Balance
                       baseline it starts from, and the saved plan.

Each is a plain module a route calls; none of them owns a blueprint, and none
imports ``routes``. That is the direction the whole app leans in — ``routes``
reads ``services``, never the other way about.
"""
