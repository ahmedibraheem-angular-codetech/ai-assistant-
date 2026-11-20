//! The quick window may open only known pages in the main window.

use assistant_desktop_lib::desktop::{allowed_route, ROUTES};

#[test]
fn known_pages_are_allowed() {
    for route in ROUTES {
        assert!(allowed_route(route), "{route}");
    }
    assert!(allowed_route("/chat/8f7eff63-805b-4d35-83bf-2f879ccc2198"));
}

#[test]
fn anything_else_is_refused() {
    for route in [
        "",
        "/nowhere",
        "javascript:alert(1)",
        "/'; alert(1); '",
        "/chat/../approvals",
        "/chat/8f7eff63-805b-4d35-83bf-2f879ccc219",   // 35 chars
        "/chat/8f7eff63-805b-4d35-83bf-2f879ccc2198'", // quote
        "/chat/zzzzzzzz-805b-4d35-83bf-2f879ccc2198",
        "https://example.com",
        "/approvals?x=1",
    ] {
        assert!(!allowed_route(route), "{route}");
    }
}
