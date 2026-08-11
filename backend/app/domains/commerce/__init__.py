"""Commerce: ticket tiers, orders, payments and tickets (spec COM-002, COM-003).

Its own schema rather than borrowing `explorer` or `publisher`, which is what
the developer and trust domains do. Money is the exception worth making: the
integration architecture asks that payment processing stay isolated from
business logic, and the rows here are financial records - never deleted, never
amended, only moved forwards through their states. Keeping them behind their
own schema boundary makes that a property of the database rather than a habit.
"""
