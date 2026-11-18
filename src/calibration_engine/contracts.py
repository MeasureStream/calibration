"""I messaggi scambiati con metrology-service: il contratto fra i due servizi.

Le classi gemelle in Kotlin stanno in metrology-service, pacchetto ``jobs.contract``. Gli esempi
in ``contracts/*.example.json`` sono identici nei due repository e i test di entrambi li
rileggono: e' cosi' che un cambiamento da una parte sola si scopre prima del deploy.

Sul filo i nomi dei campi sono in camelCase, come li scrive Jackson; qui in Python sono in
snake_case e la conversione la fa l'``alias_generator``.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

#: Versione del contratto. Un campo nuovo e' facoltativo e non la cambia.
SCHEMA_VERSION = 1

#: Topic su cui metrology-service accoda i job.
JOBS_TOPIC = "calibration-jobs"
#: Topic su cui il motore pubblica gli esiti.
RESULTS_TOPIC = "calibration-results"


class _Message(BaseModel):
    """Base comune: camelCase sul filo, campi sconosciuti ignorati (contratto che cresce)."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")

    def to_json(self) -> str:
        return self.model_dump_json(by_alias=True)

    def to_wire(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, mode="json")


class JobKind(StrEnum):
    SESSION_FIT = "SESSION_FIT"
    VERIFICATION = "VERIFICATION"
    CERT_FIT = "CERT_FIT"
    DOCUMENTS = "DOCUMENTS"


class TemplateVersionRef(_Message):
    """Un documento del registro citato per versione esatta, come fa un certificato."""

    template_id: int
    version: str


class CalibrationJob(_Message):
    """Un lavoro per il motore. Piccolo: i dati grossi si leggono via REST."""

    schema_version: int = SCHEMA_VERSION
    job_id: UUID
    kind: JobKind
    session_id: int | None = None
    certificate_id: int | None = None
    procedure: TemplateVersionRef | None = None
    reference_standard_id: int | None = None
    reference_certificate_id: int | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    requested_by: str
    requested_at: datetime


# --------------------------------------------------------------------------------------------
# Risultato
# --------------------------------------------------------------------------------------------


class JobStatus(StrEnum):
    DONE = "DONE"
    FAILED = "FAILED"


class Phase(StrEnum):
    AS_FOUND = "AS_FOUND"
    AS_LEFT = "AS_LEFT"


class Verdict(StrEnum):
    CONFORM = "CONFORM"
    GUARD_ZONE = "GUARD_ZONE"
    NON_CONFORM = "NON_CONFORM"


class ConformityTarget(StrEnum):
    """Rispetto a che cosa: le specifiche del modello (template) o l'uso (profilo) [P §7.6]."""

    SPECIFICATION = "SPECIFICATION"
    PROFILE = "PROFILE"


class SuggestedDecision(StrEnum):
    NONE = "NONE"
    ADJUST = "ADJUST"
    INVESTIGATE = "INVESTIGATE"


class Coefficient(_Message):
    id: int
    value: float
    dsi: str | None = None


class CalibrationFunction(BaseModel):
    """La funzione di taratura nella forma del blocco ``calibration`` del template.

    La formula sta sotto una chiave che si chiama come ``type`` (``"cubic_lsb": "c[0] + ..."``):
    e' la forma che sensor-manager valuta gia', quindi la si trasporta tale e quale invece di
    inventarne una seconda. Il campo dinamico vive negli extra del modello.
    """

    model_config = ConfigDict(extra="allow")

    type: str
    input: list[str] = Field(default_factory=lambda: ["x"])
    c: list[Coefficient] = Field(default_factory=list)

    @model_validator(mode="after")
    def _formula_presente(self) -> CalibrationFunction:
        if self.type != "none" and not isinstance((self.model_extra or {}).get(self.type), str):
            raise ValueError(f"manca la formula sotto la chiave '{self.type}'")
        return self

    @property
    def formula(self) -> str | None:
        return (self.model_extra or {}).get(self.type)


class CalibratedRange(_Message):
    min: float
    max: float
    dsi: str | None = None


class CalibrationPoint(_Message):
    """Una riga dei risultati: la stessa forma di ``certificate_point`` in metrology-service."""

    phase: Phase
    point_index: int
    nominal: float | None = None
    reference_value: float
    indicated_value: float
    error: float | None = None
    correction: float | None = None
    expanded_u: float | None = None
    k: float | None = None
    dsi: str | None = None


class SourceRef(_Message):
    """Il documento del registro da cui viene la tolleranza: template del sensore o profilo."""

    kind: str
    template_id: int
    version: str


class ConformityResult(_Message):
    against: ConformityTarget
    source: SourceRef
    decision_rule: str
    tolerance: float
    pfa: float | None = None
    verdict: Verdict


class ItemResult(_Message):
    """Il risultato per un esemplare: una MU e un indice di sensore."""

    calibrated_item_id: int
    mu_extended_id: int
    sensor_index: int
    function: CalibrationFunction | None = None
    covariance: list[list[float]] | None = None
    calibrated_range: CalibratedRange | None = None
    adjustment_applied: bool = False
    points: list[CalibrationPoint] = Field(default_factory=list)
    budget: dict[str, Any] = Field(default_factory=dict)
    conformity: list[ConformityResult] = Field(default_factory=list)
    suggested_decision: SuggestedDecision = SuggestedDecision.NONE

    @model_validator(mode="after")
    def _covarianza_quadrata(self) -> ItemResult:
        if self.covariance is not None and self.function is not None:
            n = len(self.function.c)
            if len(self.covariance) != n or any(len(row) != n for row in self.covariance):
                raise ValueError(f"la covarianza deve essere {n}x{n}, come i coefficienti")
        return self


class DocumentKind(StrEnum):
    DCC_XML = "DCC_XML"
    CERTIFICATE_PDF = "CERTIFICATE_PDF"
    CHART = "CHART"
    RUN_LOG = "RUN_LOG"
    BLOCK_A = "BLOCK_A"


class DocumentRef(_Message):
    """Un documento gia' caricato su metrology-service: qui viaggiano solo id e impronta."""

    document_id: int
    kind: DocumentKind
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class JobError(_Message):
    code: str
    message: str


class CalibrationResult(_Message):
    """L'esito di un job. Idempotente per ``job_id``."""

    schema_version: int = SCHEMA_VERSION
    job_id: UUID
    kind: JobKind
    status: JobStatus
    engine_version: str
    started_at: datetime
    finished_at: datetime
    error: JobError | None = None
    items: list[ItemResult] = Field(default_factory=list)
    documents: list[DocumentRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def _errore_coerente(self) -> CalibrationResult:
        if (self.status == JobStatus.FAILED) != (self.error is not None):
            raise ValueError("un job FAILED porta un errore, e solo un job FAILED lo porta")
        return self
