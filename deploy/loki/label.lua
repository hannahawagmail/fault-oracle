function add_subsystem(tag, timestamp, record)
    local msg = record["log"] or record["MESSAGE"] or ""
    local sub = "unknown"
    if msg:match("[Ee][Dd][Aa][Cc]") then sub = "edac"
    elseif msg:match("[Aa][Ee][Rr]") then sub = "aer"
    elseif msg:match("[Mm][Cc][Ee]") then sub = "mce"
    elseif msg:match("[Rr][Aa][Ss]") then sub = "ras"
    end
    record["subsystem"] = sub
    record["severity"] = msg:match("Corrected") and "correctable" or "uncorrectable"
    return 1, timestamp, record
end
