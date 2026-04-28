import sys
from pathlib import Path
from pr_agent.config_loader import get_settings

def generate_prompt_report():
    settings = get_settings()
    report = ["# PR-Agent Prompt Report", ""]
    
    # Identify prompt keys
    prompt_keys = [key for key in settings.keys() if "_prompt" in key.lower()]
    
    for key in sorted(prompt_keys):
        report.append(f"## {key.replace('_', ' ').title()}")
        section = settings.get(key)
        
        # Depending on how dynaconf loads them, they might be in a section
        # or directly accessible. 
        if isinstance(section, dict):
            for subkey, value in section.items():
                if isinstance(value, str):
                    report.append(f"- **{subkey.title()} Prompt:**")
                    report.append("```text")
                    report.append(value)
                    report.append("```")
                    report.append("")
        else:
            report.append(f"- **Prompt:**")
            report.append("```text")
            report.append(str(section))
            report.append("```")
            report.append("")
    
    # Ensure docs directory exists
    Path("docs").mkdir(exist_ok=True)
    
    with open("docs/prompt_report.md", "w", encoding="utf-8") as f:
        f.write("\n".join(report))
    print("Prompt report generated at docs/prompt_report.md")

if __name__ == "__main__":
    generate_prompt_report()
