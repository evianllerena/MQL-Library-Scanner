#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::fs::OpenOptions;
use std::io::Write;

fn startup_log(message: &str) {
    let path = std::env::temp_dir().join("mql-indicator-library-startup-error.txt");
    if let Ok(mut file) = OpenOptions::new().create(true).append(true).open(path) {
        let _ = writeln!(file, "{}", message);
    }
}

fn main() {
    startup_log(&format!("{} process entered main()", env!("CARGO_PKG_VERSION")));
    std::panic::set_hook(Box::new(|info| {
        startup_log(&format!("PANIC: {}", info));
    }));
    mql_indicator_library_lib::run();
}
