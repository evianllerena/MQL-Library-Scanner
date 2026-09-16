use rusqlite::{params, Connection};
use serde_json::{json, Value};
use std::fs::{self, OpenOptions};
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Command as ProcessCommand, Stdio};
use tauri::Emitter;

fn log_startup_error(message: &str) {
    let path = std::env::temp_dir().join("mql-indicator-library-startup-error.txt");
    if let Ok(mut file) = OpenOptions::new().create(true).append(true).open(path) {
        let _ = writeln!(file, "{}", message);
    }
}

fn open_db(path: &str) -> Result<Connection, String> {
    let conn = Connection::open(path).map_err(|e| e.to_string())?;
    conn.busy_timeout(std::time::Duration::from_secs(30)).map_err(|e| e.to_string())?;
    Ok(conn)
}

fn parse_json_text(value: String) -> Value {
    serde_json::from_str(&value).unwrap_or_else(|_| json!([]))
}

fn walk_preview(root: &Path, files: &mut Vec<String>, total: &mut u64, errors: &mut u64, max_preview: usize) {
    let entries = match fs::read_dir(root) {
        Ok(v) => v,
        Err(_) => { *errors += 1; return; }
    };
    for entry in entries {
        let entry = match entry { Ok(v) => v, Err(_) => { *errors += 1; continue; } };
        let path: PathBuf = entry.path();
        let meta = match entry.file_type() { Ok(v) => v, Err(_) => { *errors += 1; continue; } };
        if meta.is_dir() {
            walk_preview(&path, files, total, errors, max_preview);
        } else if meta.is_file() {
            *total += 1;
            if files.len() < max_preview {
                files.push(path.to_string_lossy().to_string());
            }
        }
    }
}

#[tauri::command]
fn folder_preview(path: String, max_preview: Option<usize>) -> Result<Value, String> {
    let root = Path::new(&path);
    if !root.exists() { return Err(format!("Path does not exist: {}", path)); }
    if !root.is_dir() { return Err(format!("Path is not a folder: {}", path)); }
    let mut files = Vec::new();
    let mut total: u64 = 0;
    let mut errors: u64 = 0;
    walk_preview(root, &mut files, &mut total, &mut errors, max_preview.unwrap_or(250).clamp(1, 1000));
    Ok(json!({"path":path,"total_files":total,"preview_files":files,"access_errors":errors}))
}

#[tauri::command]
fn browse_directory(path: Option<String>) -> Result<Value, String> {
    #[cfg(target_os = "windows")]
    if path.as_deref().unwrap_or("").is_empty() {
        let mut drives = Vec::new();
        for letter in b'A'..=b'Z' {
            let root = format!("{}:\\", letter as char);
            if Path::new(&root).exists() {
                drives.push(json!({"name":root.clone(),"path":root,"kind":"drive"}));
            }
        }
        return Ok(json!({"path":"This PC","parent":Value::Null,"entries":drives}));
    }

    let raw = path.unwrap_or_default();
    let dir = PathBuf::from(&raw);
    if !dir.exists() { return Err(format!("Path does not exist: {}", raw)); }
    if !dir.is_dir() { return Err(format!("Path is not a folder: {}", raw)); }

    let parent = dir.parent().map(|p| p.to_string_lossy().to_string());
    let mut entries: Vec<Value> = Vec::new();
    let rd = fs::read_dir(&dir).map_err(|e| format!("Cannot read {}: {}", raw, e))?;
    for item in rd {
        let item = match item { Ok(v) => v, Err(_) => continue };
        let file_type = match item.file_type() { Ok(v) => v, Err(_) => continue };
        let p = item.path();
        let name = item.file_name().to_string_lossy().to_string();
        let kind = if file_type.is_dir() { "folder" } else if file_type.is_file() { "file" } else { "other" };
        let size = if file_type.is_file() { item.metadata().ok().map(|m| m.len()).unwrap_or(0) } else { 0 };
        entries.push(json!({"name":name,"path":p.to_string_lossy(),"kind":kind,"size":size}));
    }
    entries.sort_by(|a,b| {
        let ak = a.get("kind").and_then(Value::as_str).unwrap_or("file");
        let bk = b.get("kind").and_then(Value::as_str).unwrap_or("file");
        let arank = if ak=="folder" || ak=="drive" {0}else{1};
        let brank = if bk=="folder" || bk=="drive" {0}else{1};
        arank.cmp(&brank).then_with(|| {
            let an = a.get("name").and_then(Value::as_str).unwrap_or("").to_lowercase();
            let bn = b.get("name").and_then(Value::as_str).unwrap_or("").to_lowercase();
            an.cmp(&bn)
        })
    });
    Ok(json!({"path":dir.to_string_lossy(),"parent":parent,"entries":entries}))
}

#[tauri::command]
fn start_scan_engine(app: tauri::AppHandle, args: Vec<String>) -> Result<Value, String> {
    if args.first().map(String::as_str) != Some("scan") {
        return Err("Only the scan command is permitted through start_scan_engine".into());
    }
    let current = std::env::current_exe().map_err(|e| format!("Cannot resolve app executable: {}", e))?;
    let dir = current.parent().ok_or_else(|| "Cannot resolve application folder".to_string())?;
    #[cfg(target_os = "windows")]
    let engine_path = dir.join("mql-engine.exe");
    #[cfg(not(target_os = "windows"))]
    let engine_path = dir.join("mql-engine");
    if !engine_path.exists() {
        return Err(format!("Scanner engine was not found at {}", engine_path.display()));
    }

    let mut cmd = ProcessCommand::new(&engine_path);
    cmd.args(&args).stdout(Stdio::piped()).stderr(Stdio::piped());
    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x08000000);
    }
    let mut child = cmd.spawn().map_err(|e| format!("Could not start scanner engine: {}", e))?;
    let stdout = child.stdout.take().ok_or_else(|| "Could not capture scanner output".to_string())?;
    let stderr = child.stderr.take().ok_or_else(|| "Could not capture scanner errors".to_string())?;

    let app_out = app.clone();
    std::thread::spawn(move || {
        for line in BufReader::new(stdout).lines().map_while(Result::ok) {
            let _ = app_out.emit("scan-engine-line", json!({"line": line}));
        }
    });
    let app_err = app.clone();
    std::thread::spawn(move || {
        for line in BufReader::new(stderr).lines().map_while(Result::ok) {
            let _ = app_err.emit("scan-engine-stderr", json!({"line": line}));
        }
    });
    std::thread::spawn(move || {
        let code = child.wait().ok().and_then(|s| s.code()).unwrap_or(-1);
        let _ = app.emit("scan-engine-done", json!({"code": code}));
    });

    Ok(json!({"started":true,"engine":engine_path.to_string_lossy()}))
}

#[tauri::command]
fn db_stats(db_path: String) -> Result<Value, String> {
    let conn = open_db(&db_path)?;
    let count = |sql: &str| -> Result<i64, String> {
        conn.query_row(sql, [], |r| r.get::<_, i64>(0)).map_err(|e| e.to_string())
    };
    let mut sources = Vec::new();
    let mut stmt = conn.prepare("SELECT path,enabled,last_scan_at FROM sources ORDER BY path").map_err(|e| e.to_string())?;
    let rows = stmt.query_map([], |r| Ok(json!({"path":r.get::<_,String>(0)?,"enabled":r.get::<_,i64>(1)?,"last_scan_at":r.get::<_,Option<String>>(2)?}))).map_err(|e| e.to_string())?;
    for row in rows { sources.push(row.map_err(|e| e.to_string())?); }
    Ok(json!({
        "total":count("SELECT COUNT(*) FROM indicators")?,
        "mq4":count("SELECT COUNT(*) FROM indicators WHERE platform='MQL4'")?,
        "mq5":count("SELECT COUNT(*) FROM indicators WHERE platform='MQL5'")?,
        "review":count("SELECT COUNT(*) FROM indicators WHERE human_verified=0 AND classification_status IN ('Needs Review','Unknown')")?,
        "duplicates":count("SELECT COUNT(*) FROM indicators WHERE duplicate_of IS NOT NULL")?,
        "favorites":count("SELECT COUNT(*) FROM indicators WHERE user_favorite=1")?,
        "verified":count("SELECT COUNT(*) FROM indicators WHERE human_verified=1")?,
        "sources":sources
    }))
}

#[tauri::command]
fn db_query(db_path:String, search:String, platform:String, category:String, review_only:bool, limit:i64, offset:i64) -> Result<Value,String> {
    let conn=open_db(&db_path)?;
    let mut clauses:Vec<String>=Vec::new();
    let mut vals:Vec<String>=Vec::new();
    if !search.trim().is_empty() {
        clauses.push("(filename LIKE ? OR primary_category LIKE ? OR secondary_categories LIKE ? OR behavior_tags LIKE ? OR techniques LIKE ? OR standard_indicators LIKE ?)".into());
        let like=format!("%{}%",search); for _ in 0..6 { vals.push(like.clone()); }
    }
    if platform!="ALL" { clauses.push("platform=?".into()); vals.push(platform); }
    if category!="ALL" { clauses.push("COALESCE(verified_primary,primary_category)=?".into()); vals.push(category); }
    if review_only { clauses.push("human_verified=0 AND classification_status IN ('Needs Review','Unknown')".into()); }
    let where_sql=if clauses.is_empty(){String::new()}else{format!(" WHERE {}",clauses.join(" AND "))};
    let total_sql=format!("SELECT COUNT(*) FROM indicators{}",where_sql);
    let mut total_stmt=conn.prepare(&total_sql).map_err(|e|e.to_string())?;
    let total:i64=total_stmt.query_row(rusqlite::params_from_iter(vals.iter()),|r|r.get(0)).map_err(|e|e.to_string())?;
    let sql=format!("SELECT id,path,filename,platform,source_structure,display_location,primary_category,secondary_categories,visual_category,behavior_tags,techniques,evidence,classification_status,review_reason,classifier_version,standard_indicators,custom_dependencies,confidence,warnings,duplicate_of,user_favorite,user_tags,human_verified,verified_primary,verified_secondary,family_fingerprint,analyzed_at FROM indicators{} ORDER BY human_verified DESC,user_favorite DESC,confidence DESC,filename COLLATE NOCASE ASC LIMIT ? OFFSET ?",where_sql);
    let mut all=vals.clone(); all.push(limit.clamp(1,500).to_string()); all.push(offset.max(0).to_string());
    let mut stmt=conn.prepare(&sql).map_err(|e|e.to_string())?;
    let mapped=stmt.query_map(rusqlite::params_from_iter(all.iter()),|r| {
        let verified: i64=r.get(22)?;
        let primary:String=r.get(6)?;
        let verified_primary:Option<String>=r.get(23)?;
        let second:String=r.get(7)?;
        let verified_second:String=r.get(24)?;
        Ok(json!({
          "id":r.get::<_,i64>(0)?,"path":r.get::<_,String>(1)?,"filename":r.get::<_,String>(2)?,"platform":r.get::<_,String>(3)?,
          "source_structure":r.get::<_,String>(4)?,"display_location":r.get::<_,String>(5)?,
          "primary_category": if verified==1 { verified_primary.unwrap_or(primary) } else { primary },
          "secondary_categories": if verified==1 { parse_json_text(verified_second) } else { parse_json_text(second) },
          "visual_category":r.get::<_,String>(8)?,"behavior_tags":parse_json_text(r.get::<_,String>(9)?),"techniques":parse_json_text(r.get::<_,String>(10)?),
          "evidence":parse_json_text(r.get::<_,String>(11)?),"classification_status": if verified==1 {"Verified".to_string()} else {r.get::<_,String>(12)?},
          "review_reason":r.get::<_,String>(13)?,"classifier_version":r.get::<_,String>(14)?,"standard_indicators":parse_json_text(r.get::<_,String>(15)?),
          "custom_dependencies":parse_json_text(r.get::<_,String>(16)?),"confidence":r.get::<_,i64>(17)?,"warnings":parse_json_text(r.get::<_,String>(18)?),
          "duplicate_of":r.get::<_,Option<String>>(19)?,"user_favorite":r.get::<_,i64>(20)?==1,"user_tags":parse_json_text(r.get::<_,String>(21)?),
          "human_verified":verified==1,"family_fingerprint":r.get::<_,String>(25)?,"analyzed_at":r.get::<_,Option<String>>(26)?
        }))
    }).map_err(|e|e.to_string())?;
    let mut rows_out=Vec::new();
    for row in mapped { rows_out.push(row.map_err(|e|e.to_string())?); }
    Ok(json!({"rows":rows_out,"total":total,"limit":limit,"offset":offset}))
}

#[tauri::command]
fn db_verify(db_path:String, indicator_id:i64, primary_category:String, secondary_categories:Vec<String>) -> Result<Value,String> {
    let mut conn=open_db(&db_path)?;
    let tx=conn.transaction().map_err(|e|e.to_string())?;
    let snapshot:(String,String,String)=tx.query_row("SELECT primary_category,evidence,family_fingerprint FROM indicators WHERE id=?",params![indicator_id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?))).map_err(|e|e.to_string())?;
    let secondary=serde_json::to_string(&secondary_categories).map_err(|e|e.to_string())?;
    tx.execute("UPDATE indicators SET human_verified=1,verified_primary=?,verified_secondary=?,verified_at=CURRENT_TIMESTAMP WHERE id=?",params![primary_category,secondary,indicator_id]).map_err(|e|e.to_string())?;
    tx.execute("INSERT INTO classification_memory(indicator_id,family_fingerprint,original_primary,corrected_primary,corrected_secondary,evidence_snapshot) VALUES(?,?,?,?,?,?)",params![indicator_id,snapshot.2,snapshot.0,primary_category,secondary,snapshot.1]).map_err(|e|e.to_string())?;
    tx.commit().map_err(|e|e.to_string())?;
    Ok(json!({"ok":true,"id":indicator_id}))
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    std::panic::set_hook(Box::new(|info| log_startup_error(&format!("panic: {}",info))));
    let result=tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_shell::init())
        .invoke_handler(tauri::generate_handler![db_stats,db_query,db_verify,folder_preview,browse_directory,start_scan_engine])
        .run(tauri::generate_context!());
    if let Err(err)=result { log_startup_error(&format!("tauri startup error: {}",err)); }
}