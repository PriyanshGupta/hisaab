"""SQLAlchemy models. Importing this package registers every table on Base.metadata."""

from hisaab.models.base import Base, new_id, utc_now
from hisaab.models.core import (
    Account,
    AccountIdentifier,
    BalanceDelta,
    BalanceObservation,
    Category,
    Payee,
    PayeeCategory,
    Source,
    Transaction,
    TransactionEdit,
    TransactionSource,
    User,
)
from hisaab.models.ops import Job, Setting, UserSession
from hisaab.models.parsing import (
    ExtractionCache,
    FormatSighting,
    GmailConnection,
    NormalizationPattern,
    ParserTemplate,
    Sender,
    SenderCandidate,
    StatementStaging,
)
from hisaab.models.views import ReadOnlyViewError, TransactionEffective

__all__ = [
    "Account", "AccountIdentifier", "BalanceDelta", "BalanceObservation", "Base", "Category",
    "ExtractionCache", "FormatSighting", "GmailConnection", "Job", "NormalizationPattern",
    "ParserTemplate", "Payee", "PayeeCategory", "ReadOnlyViewError", "Sender", "SenderCandidate",
    "Setting", "Source", "StatementStaging", "Transaction", "TransactionEdit",
    "TransactionEffective", "TransactionSource", "User", "UserSession", "new_id", "utc_now",
]
