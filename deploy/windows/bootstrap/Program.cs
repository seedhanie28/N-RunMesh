using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Text;
using System.Threading.Tasks;
using System.Windows.Forms;

[assembly: AssemblyTitle("N-RunMesh Agent Setup")]
[assembly: AssemblyDescription("Secure N-RunMesh Agent installer")]
[assembly: AssemblyCompany("N-RunMesh")]
[assembly: AssemblyProduct("N-RunMesh Agent")]
[assembly: AssemblyVersion("0.2.0.0")]
[assembly: AssemblyFileVersion("0.2.0.0")]

namespace NRunMeshInstaller
{
    internal static class Program
    {
        private const string PayloadResource = "NRunMesh.Payload";

        [STAThread]
        private static void Main(string[] args)
        {
            if (args.Length == 1 && args[0] == "--verify-payload")
            {
                string directory = Path.Combine(Path.GetTempPath(), "nrunmesh-verify-" + Guid.NewGuid().ToString("N"));
                try
                {
                    Directory.CreateDirectory(directory);
                    SetupForm.ExtractPayload(directory);
                    SetupForm.FindReleaseRoot(directory);
                    Environment.ExitCode = 0;
                }
                catch
                {
                    Environment.ExitCode = 1;
                }
                finally
                {
                    try { if (Directory.Exists(directory)) Directory.Delete(directory, true); } catch { }
                }
                return;
            }
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            using (var form = new SetupForm())
            {
                Application.Run(form);
            }
        }

        private sealed class SetupForm : Form
        {
            private readonly TextBox controller = new TextBox();
            private readonly TextBox token = new TextBox();
            private readonly TextBox agentName = new TextBox();
            private readonly ComboBox mode = new ComboBox();
            private readonly Button install = new Button();
            private readonly Label status = new Label();

            internal SetupForm()
            {
                Text = "N-RunMesh Agent Setup";
                ClientSize = new Size(560, 335);
                FormBorderStyle = FormBorderStyle.FixedDialog;
                MaximizeBox = false;
                StartPosition = FormStartPosition.CenterScreen;
                Font = new Font("Segoe UI", 9F);

                var title = new Label {
                    Text = "Install N-RunMesh Agent",
                    Font = new Font("Segoe UI Semibold", 16F),
                    AutoSize = true,
                    Location = new Point(24, 20)
                };
                var subtitle = new Label {
                    Text = "Connect this computer securely to your Controller.",
                    ForeColor = Color.DimGray,
                    AutoSize = true,
                    Location = new Point(27, 55)
                };
                Controls.Add(title);
                Controls.Add(subtitle);

                AddField("Controller URL", controller, 91, false);
                controller.Text = "http://";
                AddField("One-time setup token", token, 143, true);
                AddField("Agent name", agentName, 195, false);
                agentName.Text = Environment.MachineName;

                var modeLabel = new Label { Text = "Startup mode", AutoSize = true, Location = new Point(27, 249) };
                mode.DropDownStyle = ComboBoxStyle.DropDownList;
                mode.Items.Add("Automatic (recommended)");
                mode.Items.Add("Manual");
                mode.SelectedIndex = 0;
                mode.Location = new Point(190, 245);
                mode.Size = new Size(335, 26);
                Controls.Add(modeLabel);
                Controls.Add(mode);

                status.Text = "";
                status.AutoSize = false;
                status.Location = new Point(27, 291);
                status.Size = new Size(345, 28);
                status.ForeColor = Color.DimGray;
                Controls.Add(status);

                install.Text = "Install";
                install.Location = new Point(410, 286);
                install.Size = new Size(115, 34);
                install.BackColor = Color.FromArgb(5, 150, 105);
                install.ForeColor = Color.White;
                install.FlatStyle = FlatStyle.Flat;
                install.Click += InstallClicked;
                Controls.Add(install);
                AcceptButton = install;
            }

            private void AddField(string label, TextBox box, int top, bool secret)
            {
                Controls.Add(new Label { Text = label, AutoSize = true, Location = new Point(27, top + 4) });
                box.Location = new Point(190, top);
                box.Size = new Size(335, 26);
                box.UseSystemPasswordChar = secret;
                Controls.Add(box);
            }

            private async void InstallClicked(object sender, EventArgs e)
            {
                if (string.IsNullOrWhiteSpace(controller.Text) ||
                    string.IsNullOrWhiteSpace(token.Text) ||
                    string.IsNullOrWhiteSpace(agentName.Text))
                {
                    MessageBox.Show(this, "Controller URL, setup token, and agent name are required.",
                        "N-RunMesh Agent", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                    return;
                }

                Uri parsed;
                if (!Uri.TryCreate(controller.Text.Trim(), UriKind.Absolute, out parsed) ||
                    (parsed.Scheme != Uri.UriSchemeHttp && parsed.Scheme != Uri.UriSchemeHttps))
                {
                    MessageBox.Show(this, "Enter a valid http:// or https:// Controller URL.",
                        "N-RunMesh Agent", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                    return;
                }

                install.Enabled = false;
                status.Text = "Installing. This can take a few minutes...";
                Cursor = Cursors.WaitCursor;

                string workDir = Path.Combine(Path.GetTempPath(), "nrunmesh-agent-" + Guid.NewGuid().ToString("N"));
                string controllerUrl = controller.Text.TrimEnd('/');
                string setupToken = token.Text.Trim();
                string requestedName = agentName.Text.Trim();
                string requestedMode = mode.SelectedIndex == 0 ? "automatic" : "manual";
                try
                {
                    await Task.Run(() => RunInstallation(
                        workDir, controllerUrl, setupToken, requestedName, requestedMode));

                    token.Text = "";
                    status.Text = "Installation complete.";
                    MessageBox.Show(this,
                        "N-RunMesh Agent was installed and registered successfully.",
                        "Installation complete", MessageBoxButtons.OK, MessageBoxIcon.Information);
                    Close();
                }
                catch (Exception ex)
                {
                    status.Text = "Installation failed.";
                    MessageBox.Show(this, ex.Message, "Installation failed",
                        MessageBoxButtons.OK, MessageBoxIcon.Error);
                }
                finally
                {
                    install.Enabled = true;
                    Cursor = Cursors.Default;
                    try { if (Directory.Exists(workDir)) Directory.Delete(workDir, true); } catch { }
                }
            }

            private static void RunInstallation(
                string workDir,
                string controllerUrl,
                string setupToken,
                string requestedName,
                string requestedMode)
            {
                Directory.CreateDirectory(workDir);
                ExtractPayload(workDir);
                string releaseRoot = FindReleaseRoot(workDir);
                string script = Path.Combine(releaseRoot, "deploy", "windows", "install.ps1");
                if (!File.Exists(script))
                    throw new InvalidOperationException("The embedded Windows installer is missing.");

                var psi = new ProcessStartInfo {
                    FileName = "powershell.exe",
                    Arguments = "-NoProfile -ExecutionPolicy Bypass -File \"" + script + "\" -NonInteractive",
                    WorkingDirectory = releaseRoot,
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    RedirectStandardOutput = true,
                    RedirectStandardError = true
                };
                psi.EnvironmentVariables["NRUNMESH_CONTROLLER_URL"] = controllerUrl;
                psi.EnvironmentVariables["NRUNMESH_SETUP_TOKEN"] = setupToken;
                psi.EnvironmentVariables["NRUNMESH_AGENT_NAME"] = requestedName;
                psi.EnvironmentVariables["NRUNMESH_INSTALL_MODE"] = requestedMode;

                var stdout = new StringBuilder();
                var stderr = new StringBuilder();
                int exitCode;
                using (var process = Process.Start(psi))
                {
                    process.OutputDataReceived += (s, data) => {
                        if (data.Data != null) stdout.AppendLine(data.Data);
                    };
                    process.ErrorDataReceived += (s, data) => {
                        if (data.Data != null) stderr.AppendLine(data.Data);
                    };
                    process.BeginOutputReadLine();
                    process.BeginErrorReadLine();
                    process.WaitForExit();
                    exitCode = process.ExitCode;
                }
                if (exitCode != 0)
                    throw new InvalidOperationException(
                        stderr.Length == 0 ? stdout.ToString() : stderr.ToString());
            }

            internal static void ExtractPayload(string destination)
            {
                using (Stream source = Assembly.GetExecutingAssembly().GetManifestResourceStream(PayloadResource))
                {
                    if (source == null) throw new InvalidOperationException("Embedded payload not found.");
                    string archive = Path.Combine(destination, "payload.zip");
                    using (var output = File.Create(archive)) source.CopyTo(output);
                    ZipFile.ExtractToDirectory(archive, destination);
                    File.Delete(archive);
                }
            }

            internal static string FindReleaseRoot(string workDir)
            {
                foreach (string directory in Directory.GetDirectories(workDir, "nrunmesh-agent-*-windows-x86_64"))
                    if (File.Exists(Path.Combine(directory, "deploy", "windows", "install.ps1")))
                        return directory;
                throw new InvalidOperationException("Embedded release directory not found.");
            }
        }
    }
}
