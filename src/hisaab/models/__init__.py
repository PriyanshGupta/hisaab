"""SQLAlchemy models. Importing this package registers every table on Base.metadata."""

from hisaab.models.base import Base, new_id, utc_now
from hisaab.models.core import (
    Account,
    AccountIdentifier,
    BalanceDelta,
    BalanceObservation,
    Category,
    ExternalIdentifier,
    Payee,
    PayeeCategory,
    PayeeSuggestion,
    RawPayeeSuggestion,
    Source,
    Transaction,
    TransactionEdit,
    TransactionSource,
    User,
)
from hisaab.models.ops import Job, Setting, UserSession
from hisaab.models.parsing import (
    CorrectionDismissal,
    ExtractionCache,
    FormatSighting,
    GmailConnection,
    NormalizationPattern,
    ParserTemplate,
    ResyncNotice,
    Sender,
    SenderCandidate,
    StatementStaging,
)
from hisaab.models.views import ReadOnlyViewError, TransactionEffective

__all__: list[str] = [
    "Account", "AccountIdentifier", "BalanceDelta", "BalanceObservation", "Base", "Category", "ExternalIdentifier",
    "CorrectionDismissal", "ExtractionCache", "FormatSighting", "GmailConnection", "Job", "NormalizationPattern", "ResyncNotice",
    "ParserTemplate", "Payee", "PayeeCategory", "PayeeSuggestion", "RawPayeeSuggestion", "ReadOnlyViewError", "Sender", "SenderCandidate",
    "Setting", "Source", "StatementStaging", "Transaction", "TransactionEdit",
    "TransactionEffective", "TransactionSource", "User", "UserSession", "new_id", "utc_now",
]
