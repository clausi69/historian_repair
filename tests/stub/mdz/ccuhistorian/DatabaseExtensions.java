package mdz.ccuhistorian;

import java.sql.Timestamp;

/** Test stub with the signatures of CCU-Historian's DatabaseExtensions (needed for CREATE ALIAS). */
public class DatabaseExtensions {
    public static Long TS_TO_UNIX(Timestamp ts) {
        return ts == null ? null : ts.getTime();
    }

    public static Timestamp UNIX_TO_TS(Long unix) {
        return unix == null ? null : new Timestamp(unix);
    }
}
