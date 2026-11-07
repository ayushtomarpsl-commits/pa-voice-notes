param([string]$Out = $PSScriptRoot)
# Makes synthetic test WAVs (16 kHz, 16-bit, mono) with Windows SAPI. No microphone is used.
Add-Type -AssemblyName System.Speech

$en = @"
Okay Rahul, let me explain how we fixed the deal pipeline bug in our CRM. The issue was that when a sales user changed the stage of a deal from qualified to proposal, the deal amount was not getting saved. The Angular form was sending the amount as a string, and the dot net API was expecting a decimal, so the Dapper query was writing null into the SQL table. We fixed it in three places. First, in the Angular component, we convert the value to a number before calling the API. Second, in the API controller, we added validation, so it returns a bad request if the amount is missing. Third, we wrote a SQL script to repair the old rows where the amount was null. For deployment, run the database script first, then deploy the API, then the MCP server, and then the Angular build. Test it on QA before PROD. Priya marked this as P0, so please update the tracking sheet after the release. Also, the lead conversion screen in the ERP has the same problem, so check that next.
"@

$hinglish = @"
Rahul, Priya ne bola hai ki lead conversion wala bug P0 hai. Jab user lead ko deal mein convert karta hai, toh contact ka email copy nahi ho raha. Maine API mein check kiya, stored procedure mein ek column missing tha. Kal QA pe deploy karna hai, phir PROD pe. Deploy ke baad sheet mein status update kar dena.
"@

$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)

function Say([string]$voice, [string]$text, [string]$file, [int]$rate) {
    $s = New-Object System.Speech.Synthesis.SpeechSynthesizer
    $s.SelectVoice($voice)
    $s.Rate = $rate
    $s.SetOutputToWaveFile($file, $fmt)
    $s.Speak($text)
    $s.SetOutputToNull()
    $s.Dispose()
}

Say "Microsoft David Desktop" $en (Join-Path $Out "en_crm_bugfix.wav") 0
Say "Microsoft Zira Desktop" $hinglish (Join-Path $Out "hinglish_romanized_zira.wav") 0
Set-Content -Path (Join-Path $Out "en_crm_bugfix.txt") -Value $en.Trim() -Encoding UTF8
Set-Content -Path (Join-Path $Out "hinglish_romanized_zira.txt") -Value $hinglish.Trim() -Encoding UTF8
Get-ChildItem $Out -Filter *.wav | Select-Object Name, Length
