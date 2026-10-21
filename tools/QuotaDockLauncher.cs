// A fixed installation entry; application versions never replace this executable.
using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Runtime.Serialization;
using System.Runtime.Serialization.Json;
using System.Text;
using System.Windows.Forms;
using System.Reflection;

[assembly: AssemblyTitle("QuotaDock")]
[assembly: AssemblyDescription("QuotaDock stable launcher")]
[assembly: AssemblyVersion("1.0.0.0")]
[assembly: AssemblyFileVersion("1.0.0.0")]

[DataContract]
class ActiveVersion {
    [DataMember(Name = "executable", IsRequired = true)] public string Executable { get; set; }
}

static class QuotaDockLauncher {
    static string Quote(string value) {
        if (value.Length > 0 && value.IndexOfAny(new[]{' ', '\t', '\n', '\v', '"'}) < 0) return value;
        var output = new StringBuilder("\"");
        int slashes = 0;
        foreach (char c in value) {
            if (c == '\\') { slashes++; continue; }
            output.Append('\\', c == '"' ? slashes * 2 + 1 : slashes);
            output.Append(c);
            slashes = 0;
        }
        output.Append('\\', slashes * 2);
        return output.Append('"').ToString();
    }

    [STAThread]
    static int Main(string[] args) {
        bool checkOnly = args.Length == 1 && args[0] == "--check";
        try {
            string root = Path.GetFullPath(AppDomain.CurrentDomain.BaseDirectory);
            string manifest = Path.Combine(root, "current.json");
            if (new FileInfo(manifest).Length > 65536) throw new InvalidDataException("Manifest is too large.");
            ActiveVersion current;
            using (var file = new FileStream(manifest, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
                current = (ActiveVersion)new DataContractJsonSerializer(typeof(ActiveVersion)).ReadObject(file);
            if (String.IsNullOrWhiteSpace(current.Executable) || Path.IsPathRooted(current.Executable)
                || current.Executable.Contains(":")) throw new InvalidDataException("Manifest target must be relative.");
            string target = Path.GetFullPath(Path.Combine(root, current.Executable));
            string versions = Path.Combine(root, "versions") + Path.DirectorySeparatorChar;
            if (!target.StartsWith(versions, StringComparison.OrdinalIgnoreCase)
                || !String.Equals(Path.GetFileName(target), "QuotaDock.exe", StringComparison.OrdinalIgnoreCase)
                || !File.Exists(target)) throw new InvalidDataException("Target is missing or outside versions: " + target);
            // A manifest must not route through a junction/symlink outside the installation.
            for (var parent = new DirectoryInfo(Path.GetDirectoryName(target));
                 parent != null && !String.Equals(parent.FullName.TrimEnd('\\'), root.TrimEnd('\\'), StringComparison.OrdinalIgnoreCase);
                 parent = parent.Parent)
                if ((parent.Attributes & FileAttributes.ReparsePoint) != 0) throw new InvalidDataException("Reparse directory: " + parent.FullName);
            if ((File.GetAttributes(target) & FileAttributes.ReparsePoint) != 0) throw new InvalidDataException("Reparse target.");
            if (checkOnly) return 0;
            var start = new ProcessStartInfo(target, String.Join(" ", args.Select(Quote))) {
                WorkingDirectory = Path.GetDirectoryName(target), UseShellExecute = false, CreateNoWindow = true
            };
            // The child is an independent PyInstaller application, never this launcher's runtime.
            start.EnvironmentVariables["PYINSTALLER_RESET_ENVIRONMENT"] = "1";
            Process.Start(start);
            return 0;
        } catch (Exception error) {
            if (checkOnly) Console.Error.WriteLine(error.ToString());
            if (!checkOnly)
                MessageBox.Show("無法開啟目前安裝的版本。請重新安裝最新正式版，原有指令與用量資料會保留。",
                                "QuotaDock", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }
}
