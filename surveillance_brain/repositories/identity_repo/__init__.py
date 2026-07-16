"""
repositories/identity_repo package — CRUD for identities / employees / visitors
/ unknown_cases (thin SQLAlchemy wrappers; the service layer owns transactions).

Split from the original single identity_repo.py into per-table modules. Every
public function is re-exported here, so existing calls like
`identity_repo.insert_employee(...)` and `from repositories.identity_repo import
find_identity_by_query` keep working unchanged.
"""

from .labels import _serialize_label_allocation
from .identities import (
    fetch_identity_by_id,
    fetch_identity_by_label,
    list_visitor_identities,
    create_identity,
    update_identity_type_and_label,
)
from .visitors import (
    next_visitor_seq,
    insert_visitor,
    delete_visitor,
    fetch_visitor,
    get_visitor_flags,
    set_visitor_flags,
    confirm_visitor,
    is_confirmed_visitor,
)
from .employees import (
    next_employee_seq,
    insert_employee,
    delete_employee,
    fetch_employee,
    list_employees,
    fetch_employee_by_external_id,
)
from .lookups import get_name_for_identity
from .unknown_cases import (
    next_unknown_seq,
    insert_unknown_case,
    fetch_unknown_case_by_track,
)
from .search import find_identity_by_query, search_identities

__all__ = [
    "fetch_identity_by_id", "fetch_identity_by_label", "list_visitor_identities",
    "create_identity", "update_identity_type_and_label",
    "next_visitor_seq", "insert_visitor", "delete_visitor", "fetch_visitor",
    "get_visitor_flags", "set_visitor_flags", "confirm_visitor", "is_confirmed_visitor",
    "next_employee_seq", "insert_employee", "delete_employee", "fetch_employee",
    "list_employees", "fetch_employee_by_external_id",
    "get_name_for_identity",
    "next_unknown_seq", "insert_unknown_case", "fetch_unknown_case_by_track",
    "find_identity_by_query", "search_identities",
]
