if (-not ('WMIBench.TerminalSessions' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Runtime.InteropServices;
namespace WMIBench {
    public class TerminalSession {
        public int SessionId {get;set;}
        public string Station {get;set;}
        public string User {get;set;}
        public string State {get;set;}
    }
    public static class TerminalSessions {
        [StructLayout(LayoutKind.Sequential)]
        struct SessionInfo { public int Id; public IntPtr Station; public int State; }
        [DllImport("wtsapi32.dll", EntryPoint="WTSEnumerateSessionsW", SetLastError=true)]
        static extern bool Enumerate(IntPtr server, int reserved, int version, out IntPtr data, out int count);
        [DllImport("wtsapi32.dll", EntryPoint="WTSQuerySessionInformationW", SetLastError=true)]
        static extern bool Query(IntPtr server, int id, int info, out IntPtr data, out int bytes);
        [DllImport("wtsapi32.dll")] static extern void WTSFreeMemory(IntPtr data);
        [DllImport("wtsapi32.dll", SetLastError=true)]
        static extern bool WTSDisconnectSession(IntPtr server, int id, bool wait);
        [DllImport("wtsapi32.dll", SetLastError=true)]
        static extern bool WTSLogoffSession(IntPtr server, int id, bool wait);
        static string Text(int id, int info) {
            IntPtr data; int bytes;
            if (!Query(IntPtr.Zero, id, info, out data, out bytes))
                throw new Win32Exception(Marshal.GetLastWin32Error());
            try { return bytes > 2 ? Marshal.PtrToStringUni(data) : ""; }
            finally { WTSFreeMemory(data); }
        }
        public static TerminalSession[] List() {
            IntPtr data; int count;
            if (!Enumerate(IntPtr.Zero, 0, 1, out data, out count))
                throw new Win32Exception(Marshal.GetLastWin32Error());
            try {
                var rows = new List<TerminalSession>();
                string[] states = {"Active", "Connected", "ConnectQuery", "Shadow", "Disconnected", "Idle", "Listen", "Reset", "Down", "Init"};
                int size = Marshal.SizeOf(typeof(SessionInfo));
                for (int i = 0; i < count; i++) {
                    var item = (SessionInfo)Marshal.PtrToStructure(IntPtr.Add(data, i * size), typeof(SessionInfo));
                    string user = Text(item.Id, 5), domain = Text(item.Id, 7);
                    rows.Add(new TerminalSession { SessionId=item.Id, Station=Marshal.PtrToStringUni(item.Station),
                        User=user.Length == 0 ? "" : domain + "\\" + user,
                        State=item.State >= 0 && item.State < states.Length ? states[item.State] : item.State.ToString() });
                }
                return rows.ToArray();
            } finally { WTSFreeMemory(data); }
        }
        public static void Change(int id, string expectedUser, bool logoff) {
            if (id <= 0 || String.IsNullOrEmpty(expectedUser)) throw new ArgumentException("Select a user session.");
            string user = Text(id, 7) + "\\" + Text(id, 5);
            if (!String.Equals(user, expectedUser, StringComparison.Ordinal))
                throw new InvalidOperationException("Session changed. Refresh the session list.");
            bool ok = logoff ? WTSLogoffSession(IntPtr.Zero, id, false) : WTSDisconnectSession(IntPtr.Zero, id, false);
            if (!ok) throw new Win32Exception(Marshal.GetLastWin32Error());
        }
    }
}
'@
}
