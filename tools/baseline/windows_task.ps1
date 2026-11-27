param([Parameter(Mandatory=$true)][string]$Request, [ValidateSet('start','status','remove')][string]$Action='start')
$ErrorActionPreference='Stop'
$data=Get-Content -LiteralPath $Request -Raw -Encoding UTF8 | ConvertFrom-Json
if($data.task_name -notmatch '^Pixlate-GS03-[a-f0-9]{32}$'){throw 'Invalid task name'}
$service=New-Object -ComObject 'Schedule.Service'
$service.Connect()
$root=$service.GetFolder('\')
if($Action -eq 'status'){
    $task=$root.GetTask($data.task_name)
    @{state=[int]$task.State;last_result=$task.LastTaskResult;last_run=$task.LastRunTime.ToString('o')}|ConvertTo-Json -Compress
    exit
}
if($Action -eq 'remove'){
    $task=$root.GetTask($data.task_name)
    if([int]$task.State -eq 4){throw 'Running task must not be removed'}
    $root.DeleteTask($data.task_name,0)
    exit
}
$definition=$service.NewTask(0)
$definition.RegistrationInfo.Description='Pixlate GS03 explicit one-shot supervisor; no model retry'
$definition.Principal.UserId=[System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$definition.Principal.LogonType=3 # InteractiveToken: password stored nowhere
$definition.Principal.RunLevel=0
$definition.Settings.Enabled=$true
$definition.Settings.Hidden=$true
$definition.Settings.AllowDemandStart=$true
$definition.Settings.DisallowStartIfOnBatteries=$false
$definition.Settings.StopIfGoingOnBatteries=$false
$definition.Settings.ExecutionTimeLimit='PT0S'
$definition.Settings.RestartCount=0
$definition.Settings.MultipleInstances=2 # IgnoreNew
$act=$definition.Actions.Create(0)
$act.Path=$data.supervisor_executable
$act.Arguments=$data.supervisor_arguments
$act.WorkingDirectory=$data.cwd
# TASK_CREATE only: never replace a pre-existing task.
$task=$root.RegisterTaskDefinition($data.task_name,$definition,2,$definition.Principal.UserId,$null,3)
$definition.XmlText | Set-Content -LiteralPath (Join-Path $data.output 'task-definition.xml') -Encoding UTF8
$running=$task.Run($null)
@{task_name=$data.task_name;instance_guid=$running.InstanceGuid;state=[int]$running.State}|ConvertTo-Json -Compress
