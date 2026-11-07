"""STTM authoring: sessions, their tables and the sttm-agent's tools.

Why the sttm-agent works through tools instead of writing the tables itself:

- Memory. A subagent remembers nothing between tasks, and a session runs over
  many analyst messages: inputs, three reviews, corrections, approval. The
  session lives in the conversation's state (``sttm_sessions``), and only these
  tools change it.
- No invented content, enforced in code rather than asked of the model. Rows
  come only from the specification's attributes, the declared derived fields and
  the audit fields a rule file lists. A value given as the analyst's has to be in
  their own words, and a rule value has to cite a rule file that is present.
  What nothing decides stays unresolved.
- Reviews and approval, enforced in code. A review opened in one analyst message
  can only be accepted in a later one. Approval needs the final summary and, over
  failed checks, the analyst's reason; an approved session is read-only, and
  changing it takes a new version.
- The same output every time. The Data Dictionary, both STTMs, validation, the
  export and the workbooks are built from the recorded session, so a correction
  changes only the cells it names.

The tools (:mod:`.tools`): sttm_start_session, sttm_record_input, sttm_update
(every value, each checked against its source), sttm_advance (the steps,
reviews and approval), sttm_show (views and the JSON export) and sttm_workbook
(the Data Dictionary and STTM workbooks, laid out as the analyst asks, kept a day
for them to download: :mod:`.workbook`, :mod:`.files`).
"""

from v1.core.tools.sttm.tools import STTM_TOOL_NAMES, STTM_TOOLS

__all__ = ["STTM_TOOLS", "STTM_TOOL_NAMES"]
