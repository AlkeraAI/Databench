from enum import StrEnum


class ChatPublisherStateUpdateRefusalKindType0(StrEnum):
    FILES_UNREACHABLE = "files_unreachable"
    FILES_UNSAVED = "files_unsaved"
    FOLDER_GONE = "folder_gone"
    MOVING = "moving"
    NOT_ALLOWED = "not_allowed"
    RESUME_FAILED = "resume_failed"
    SANDBOX_REFUSED = "sandbox_refused"
    START_FAILED = "start_failed"
    TRANSCRIPT_UNOPENED = "transcript_unopened"
    WORKSPACE_ELSEWHERE = "workspace_elsewhere"
    WORKSPACE_UNSERVABLE = "workspace_unservable"

    def __str__(self) -> str:
        return str(self.value)
