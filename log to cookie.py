#!/usr/bin/env python3
"""
Akaza Log Tool - Smart Cookie Extractor
Extracts cookies from large log files by domain with interactive menu
"""

import re
import sys
import argparse
from pathlib import Path
from typing import List, Tuple, Optional, Dict
import os

# Try to import tkinter for file dialog
try:
    import tkinter as tk
    from tkinter import filedialog
    TKINTER_AVAILABLE = True
except ImportError:
    TKINTER_AVAILABLE = False


def print_banner():
    """Print the Akaza Log Tool banner"""
    banner = """
╔═══════════════════════════════════════════════════════════════════╗
║                                                                   ║
║     █████╗ ██╗  ██╗ █████╗ ███████╗ █████╗     ██╗      ██████╗  ║
║    ██╔══██╗██║ ██╔╝██╔══██╗╚══███╔╝██╔══██╗    ██║     ██╔═══██╗ ║
║    ███████║█████╔╝ ███████║  ███╔╝ ███████║    ██║     ██║   ██║ ║
║    ██╔══██║██╔═██╗ ██╔══██║ ███╔╝  ██╔══██║    ██║     ██║   ██║ ║
║    ██║  ██║██║  ██╗██║  ██║███████╗██║  ██║    ███████╗╚██████╔╝ ║
║    ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝    ╚══════╝ ╚═════╝  ║
║                                                                   ║
║                    SMART COOKIE EXTRACTOR                         ║
║                  Extract Cookies by Domain                        ║
║                                                                   ║
╚═══════════════════════════════════════════════════════════════════╝
"""
    print(banner)


def print_menu():
    """Print the main menu"""
    print("\n" + "="*70)
    print("                          MAIN MENU")
    print("="*70)
    print("\n  [1] Extract Cookies (Interactive Mode)")
    print("  [2] Extract Cookies (Command Line Mode)")
    print("  [3] View Statistics of Previous Extraction")
    print("  [4] Help & Documentation")
    print("  [5] Exit")
    print("\n" + "="*70)


def get_directory_choice():
    """Ask user how they want to select directory"""
    print("\n" + "-"*70)
    print("  SELECT DIRECTORY METHOD")
    print("-"*70)
    print("\n  [1] Browse with File Dialog (GUI)")
    print("  [2] Enter Path Manually")
    print("  [3] Use Current Directory")
    print("\n" + "-"*70)
    
    while True:
        choice = input("\n  Enter your choice (1-3): ").strip()
        if choice in ['1', '2', '3']:
            return choice
        print("  ❌ Invalid choice. Please enter 1, 2, or 3.")


def select_directory_gui():
    """Open GUI file dialog to select directory"""
    if not TKINTER_AVAILABLE:
        print("\n  ❌ Tkinter not available. Please install it or use manual entry.")
        return None
    
    try:
        root = tk.Tk()
        root.withdraw()  # Hide the main window
        root.attributes('-topmost', True)  # Bring dialog to front
        
        print("\n  📂 Opening file dialog... (check your taskbar if you don't see it)")
        directory = filedialog.askdirectory(
            title="Select Log Directory",
            mustexist=True
        )
        root.destroy()
        
        if directory:
            return directory
        else:
            print("\n  ⚠️  No directory selected.")
            return None
    except Exception as e:
        print(f"\n  ❌ Error opening file dialog: {e}")
        return None


def select_directory():
    """Let user select directory using their preferred method"""
    choice = get_directory_choice()
    
    if choice == '1':
        # GUI file dialog
        directory = select_directory_gui()
        if directory:
            print(f"\n  ✓ Selected: {directory}")
            return directory
        else:
            print("\n  Falling back to manual entry...")
            choice = '2'
    
    if choice == '2':
        # Manual entry
        while True:
            directory = input("\n  Enter directory path: ").strip().strip('"').strip("'")
            if os.path.exists(directory):
                print(f"\n  ✓ Directory found: {directory}")
                return directory
            else:
                print(f"\n  ❌ Directory not found: {directory}")
                retry = input("  Try again? (y/n): ").strip().lower()
                if retry != 'y':
                    return None
    
    if choice == '3':
        # Current directory
        directory = os.getcwd()
        print(f"\n  ✓ Using current directory: {directory}")
        return directory
    
    return None


def interactive_mode():
    """Run the tool in interactive mode"""
    print("\n" + "="*70)
    print("                    INTERACTIVE EXTRACTION MODE")
    print("="*70)
    
    # Get domain
    print("\n  STEP 1: Enter Domain")
    print("  " + "-"*66)
    while True:
        domain = input("\n  Enter domain to extract (e.g., youtube.com, spotify.com): ").strip()
        if domain and '.' in domain:
            break
        print("  ❌ Invalid domain. Must contain a dot (e.g., example.com)")
    
    # Get directory
    print("\n  STEP 2: Select Log Directory")
    print("  " + "-"*66)
    directory = select_directory()
    if not directory:
        print("\n  ❌ No directory selected. Returning to main menu...")
        return
    
    # Get output options
    print("\n  STEP 3: Output Options")
    print("  " + "-"*66)
    print("\n  [1] Real-time save (separate files for each source)")
    print("  [2] Combined file only")
    print("  [3] Both real-time and combined")
    
    while True:
        output_choice = input("\n  Enter your choice (1-3): ").strip()
        if output_choice in ['1', '2', '3']:
            break
        print("  ❌ Invalid choice. Please enter 1, 2, or 3.")
    
    realtime = output_choice in ['1', '3']
    combined = output_choice in ['2', '3']
    
    # Get output filename if combined
    output_file = None
    if combined:
        default_name = f"{domain.replace('.', '_')}_cookies.txt"
        output_file = input(f"\n  Enter output filename (default: {default_name}): ").strip()
        if not output_file:
            output_file = default_name
    
    # Get results directory if realtime
    results_dir = "results"
    if realtime:
        custom_dir = input(f"\n  Enter results directory (default: results): ").strip()
        if custom_dir:
            results_dir = custom_dir
    
    # Ask for statistics
    show_stats = input("\n  Show statistics? (y/n, default: y): ").strip().lower()
    show_stats = show_stats != 'n'
    
    # Confirm and execute
    print("\n" + "="*70)
    print("                         EXTRACTION SUMMARY")
    print("="*70)
    print(f"\n  Domain:           {domain}")
    print(f"  Directory:        {directory}")
    print(f"  Real-time save:   {'Yes' if realtime else 'No'}")
    if realtime:
        print(f"  Results folder:   {results_dir}/{domain}/")
    if combined:
        print(f"  Combined file:    {output_file}")
    print(f"  Show statistics:  {'Yes' if show_stats else 'No'}")
    print("\n" + "="*70)
    
    confirm = input("\n  Proceed with extraction? (y/n): ").strip().lower()
    if confirm != 'y':
        print("\n  ❌ Extraction cancelled.")
        return
    
    # Build command arguments
    print("\n" + "="*70)
    print("                      STARTING EXTRACTION...")
    print("="*70 + "\n")
    
    # Create extractor and run
    extractor = SmartCookieExtractor(domain)
    
    try:
        results = extractor.extract_from_directory(
            directory,
            output_dir=results_dir if realtime else None,
            realtime_save=realtime
        )
        
        # Flatten results
        all_cookies = []
        for filepath, cookies in results.items():
            for cookie in cookies:
                cookie["source_file"] = filepath
                all_cookies.append(cookie)
        
        print(f"\n✓ Found {len(all_cookies):,} cookies from {domain}", file=sys.stderr)
        
        if realtime:
            unique_sources = len(set(cookie["source_file"] for cookie in all_cookies))
            print(f"✓ Saved to {results_dir}/{domain}/ ({unique_sources} files)")
        
        # Save combined file if requested
        if combined and output_file:
            with open(output_file, "w", encoding="utf-8") as f:
                for cookie in all_cookies:
                    f.write(f"{cookie['domain']}\t{cookie['flag']}\t{cookie['path']}\t"
                           f"{cookie['secure']}\t{cookie['expiration']}\t"
                           f"{cookie['name']}\t{cookie['value']}\n")
            print(f"✓ Combined file saved to {output_file}")
        
        # Show statistics
        if show_stats and all_cookies:
            print("\n" + "="*70)
            print("                           STATISTICS")
            print("="*70)
            
            name_counts = {}
            for cookie in all_cookies:
                name = cookie["name"]
                name_counts[name] = name_counts.get(name, 0) + 1
            
            file_counts = {}
            for cookie in all_cookies:
                source = cookie["source_file"]
                file_counts[source] = file_counts.get(source, 0) + 1
            
            print(f"\n  Total cookies:        {len(all_cookies):,}")
            print(f"  Unique cookie names:  {len(name_counts)}")
            print(f"  Files with matches:   {len(file_counts)}")
            
            print("\n  Top 10 Cookie Names:")
            print("  " + "-"*66)
            for i, (name, count) in enumerate(sorted(name_counts.items(), key=lambda x: -x[1])[:10], 1):
                print(f"  {i:2d}. {name:40s} {count:>10,}")
        
        print("\n" + "="*70)
        print("                    ✓ EXTRACTION COMPLETE!")
        print("="*70)
        
    except Exception as e:
        print(f"\n❌ Error during extraction: {e}", file=sys.stderr)
    
    input("\n  Press Enter to return to main menu...")


def show_help():
    """Show help and documentation"""
    print("\n" + "="*70)
    print("                      HELP & DOCUMENTATION")
    print("="*70)
    
    help_text = """
  WHAT IS AKAZA LOG TOOL?
  ----------------------
  Akaza Log Tool extracts cookies from browser log files by domain name.
  It processes Netscape-format cookie files and organizes them for easy use.

  HOW IT WORKS:
  ------------
  1. Select a domain (e.g., youtube.com, spotify.com)
  2. Choose the log directory containing cookie files
  3. The tool scans all "Cookies/*.txt" files recursively
  4. Extracts only cookies matching your domain
  5. Saves them in organized, ready-to-use files

  OUTPUT MODES:
  ------------
  • Real-time Save: Creates separate files, each containing all cookies
                    from one source (keeps cookie sets working together)
  
  • Combined File:  All cookies from all sources in one file
  
  • Both:          Get both organized files AND a combined file

  FILE STRUCTURE:
  --------------
  results/
    └── domain.com/
        ├── akaza_domain.com_1.txt  (all cookies from source #1)
        ├── akaza_domain.com_2.txt  (all cookies from source #2)
        └── ...

  COMMAND LINE USAGE:
  ------------------
  python extract_cookies_smart.py <domain> <directory> [options]

  Options:
    --realtime, -r              Save to separate files in real-time
    --results-dir DIR           Custom results directory
    -o FILE                     Save combined output file
    -s                          Show statistics
    -q                          Quiet mode
    -f FORMAT                   Output format (netscape/json/simple)

  EXAMPLES:
  --------
  Interactive:
    python extract_cookies_smart.py

  Command line:
    python extract_cookies_smart.py youtube.com "Logs_8 April" --realtime -s
    python extract_cookies_smart.py spotify.com "C:/logs" -o spotify.txt
"""
    print(help_text)
    print("="*70)
    input("\n  Press Enter to return to main menu...")


def main_menu():
    """Main menu loop"""
    print_banner()
    
    while True:
        print_menu()
        choice = input("\n  Enter your choice (1-5): ").strip()
        
        if choice == '1':
            interactive_mode()
        elif choice == '2':
            print("\n  Command Line Mode:")
            print("  Use: python extract_cookies_smart.py <domain> <directory> [options]")
            print("  Run with --help for more information")
            input("\n  Press Enter to return to main menu...")
        elif choice == '3':
            print("\n  Feature coming soon!")
            input("\n  Press Enter to return to main menu...")
        elif choice == '4':
            show_help()
        elif choice == '5':
            print("\n" + "="*70)
            print("           Thank you for using Akaza Log Tool!")
            print("="*70 + "\n")
            sys.exit(0)
        else:
            print("\n  ❌ Invalid choice. Please enter a number between 1 and 5.")
            input("\n  Press Enter to continue...")


class SmartCookieExtractor:
    """Efficiently extracts cookies from Netscape cookie format files"""

    def __init__(self, domain: str, patterns: Optional[List[str]] = None):
        """
        Initialize extractor

        Args:
            domain: Domain to filter cookies (e.g., 'spotify.com')
            patterns: Optional regex patterns for additional filtering
        """
        self.domain = domain.lower()
        self.patterns = patterns or []
        self.domain_pattern = re.compile(rf"({re.escape(self.domain)})", re.IGNORECASE)

    def extract_from_file(self, filepath: str) -> List[Dict[str, str]]:
        """
        Extract cookies from Netscape format cookie file

        Args:
            filepath: Path to cookie file

        Returns:
            List of cookie dictionaries
        """
        results = []
        filepath = Path(filepath)

        if not filepath.exists():
            raise FileNotFoundError(f"File not found: {filepath}")

        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                
                # Skip empty lines and comments
                if not line or line.startswith("#"):
                    continue

                cookie = self.parse_cookie_line(line)
                if cookie and self._matches_domain(cookie["domain"]):
                    results.append(cookie)

        return results

    def parse_cookie_line(self, line: str) -> Optional[Dict[str, str]]:
        """
        Parse a Netscape cookie format line
        Format: domain flag path secure expiration name value

        Args:
            line: Cookie line to parse

        Returns:
            Dictionary with cookie data or None if invalid
        """
        parts = line.split("\t")
        
        if len(parts) < 7:
            return None

        try:
            return {
                "domain": parts[0].strip(),
                "flag": parts[1].strip(),
                "path": parts[2].strip(),
                "secure": parts[3].strip(),
                "expiration": parts[4].strip(),
                "name": parts[5].strip(),
                "value": parts[6].strip(),
            }
        except Exception:
            return None

    def _matches_domain(self, cookie_domain: str) -> bool:
        """Check if cookie domain matches the target domain"""
        cookie_domain = cookie_domain.lower().lstrip(".")
        target_domain = self.domain.lstrip(".")
        
        # Exact match or subdomain match
        return cookie_domain == target_domain or cookie_domain.endswith("." + target_domain) or target_domain.endswith("." + cookie_domain)

    def extract_from_directory(self, directory: str, output_dir: Optional[str] = None, 
                              realtime_save: bool = False) -> Dict[str, List[Dict[str, str]]]:
        """
        Recursively extract cookies from all cookie files in directory

        Args:
            directory: Path to directory containing cookie files
            output_dir: Base output directory for real-time saving
            realtime_save: If True, save cookies in real-time to separate files

        Returns:
            Dictionary mapping file paths to cookie lists
        """
        results = {}
        directory = Path(directory)

        if not directory.exists():
            raise FileNotFoundError(f"Directory not found: {directory}")

        # Setup output directory structure if real-time saving
        domain_output_dir = None
        if realtime_save and output_dir:
            domain_output_dir = Path(output_dir) / self.domain
            domain_output_dir.mkdir(parents=True, exist_ok=True)
            print(f"Saving cookies to: {domain_output_dir}", file=sys.stderr)

        # Find all .txt files in Cookies subdirectories
        cookie_files = []
        try:
            cookie_files = list(directory.rglob("Cookies/*.txt"))
        except Exception as e:
            print(f"Warning: Error scanning directory: {e}", file=sys.stderr)
            # Try alternative approach
            try:
                for item in directory.iterdir():
                    if item.is_dir():
                        cookies_dir = item / "Cookies"
                        if cookies_dir.exists():
                            cookie_files.extend(cookies_dir.glob("*.txt"))
            except Exception as e2:
                print(f"Error: Could not scan directory: {e2}", file=sys.stderr)
        
        print(f"Found {len(cookie_files)} cookie files to process", file=sys.stderr)

        file_counter = 1
        for i, filepath in enumerate(cookie_files, 1):
            try:
                cookies = self.extract_from_file(str(filepath))
                if cookies:
                    results[str(filepath)] = cookies
                    
                    # Real-time save: all cookies from same source file go to one output file
                    if realtime_save and domain_output_dir:
                        output_filename = f"akaza_{self.domain}_{file_counter}.txt"
                        output_path = domain_output_dir / output_filename
                        
                        # Write all cookies from this source file together
                        with open(output_path, "w", encoding="utf-8") as f:
                            for cookie in cookies:
                                f.write(f"{cookie['domain']}\t{cookie['flag']}\t{cookie['path']}\t"
                                       f"{cookie['secure']}\t{cookie['expiration']}\t"
                                       f"{cookie['name']}\t{cookie['value']}\n")
                        
                        file_counter += 1
                    
                if i % 100 == 0:
                    saved_count = file_counter - 1 if realtime_save else 0
                    if realtime_save:
                        print(f"Processed {i}/{len(cookie_files)} files... (Saved {saved_count} cookie files)", file=sys.stderr)
                    else:
                        print(f"Processed {i}/{len(cookie_files)} files...", file=sys.stderr)
            except Exception as e:
                # Skip files that cause errors
                pass

        return results


def main():
    parser = argparse.ArgumentParser(
        description="Smart Cookie Extractor - Extract cookies from Netscape format files by domain",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Extract spotify cookies and save in real-time to separate files
  python extract_cookies_smart.py spotify.com "Logs_8 April" --realtime
  
  # Extract youtube cookies with statistics
  python extract_cookies_smart.py youtube.com "Logs_8 April" -o youtube.txt -s
  
  # Extract and save to custom results folder
  python extract_cookies_smart.py google.com "Logs_8 April" --realtime --results-dir "my_results"
        """,
    )

    parser.add_argument("domain", help="Domain to filter (e.g., spotify.com)")
    parser.add_argument("directory", help="Log directory to process")
    parser.add_argument("-o", "--output", help="Output file for combined results (optional)")
    parser.add_argument(
        "-r", "--realtime", action="store_true", 
        help="Save each cookie to separate file in real-time"
    )
    parser.add_argument(
        "--results-dir", default="results",
        help="Base directory for real-time saved files (default: results)"
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true", help="Suppress progress output"
    )
    parser.add_argument(
        "-s", "--stats", action="store_true", help="Show detailed statistics"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Show all cookie details"
    )
    parser.add_argument(
        "-f", "--format", choices=["netscape", "json", "simple"], default="netscape",
        help="Output format for combined file (default: netscape)"
    )

    args = parser.parse_args()

    # Validate domain
    if not args.domain or "." not in args.domain:
        print("Error: Domain must contain a dot (e.g., spotify.com)", file=sys.stderr)
        sys.exit(1)

    # Initialize extractor
    extractor = SmartCookieExtractor(args.domain)

    # Process directory
    print(f"Searching for {args.domain} cookies in {args.directory}...", file=sys.stderr)
    
    try:
        results = extractor.extract_from_directory(
            args.directory, 
            output_dir=args.results_dir if args.realtime else None,
            realtime_save=args.realtime
        )
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Flatten results
    all_cookies = []
    for filepath, cookies in results.items():
        for cookie in cookies:
            cookie["source_file"] = filepath
            all_cookies.append(cookie)

    if not args.quiet:
        print(f"\n✓ Found {len(all_cookies):,} cookies from {args.domain}", file=sys.stderr)
        if args.realtime:
            # Count unique source files that had matches
            unique_sources = len(set(cookie["source_file"] for cookie in all_cookies))
            print(f"✓ Saved to {args.results_dir}/{args.domain}/ ({unique_sources} files, each containing all cookies from one source)", file=sys.stderr)

    # Format output for combined file (if requested)
    if args.output:
        output_lines = []
        
        if args.format == "netscape":
            for cookie in all_cookies:
                output_lines.append(
                    f"{cookie['domain']}\t{cookie['flag']}\t{cookie['path']}\t"
                    f"{cookie['secure']}\t{cookie['expiration']}\t{cookie['name']}\t{cookie['value']}"
                )
        elif args.format == "json":
            import json
            output_lines.append(json.dumps(all_cookies, indent=2))
        else:  # simple
            for cookie in all_cookies:
                output_lines.append(f"{cookie['name']}={cookie['value']}")

        # Write combined output
        with open(args.output, "w", encoding="utf-8") as f:
            f.write("\n".join(output_lines))
        print(f"✓ Combined file saved to {args.output}", file=sys.stderr)

    # Show statistics
    if args.stats:
        print("\n" + "=" * 60, file=sys.stderr)
        print("STATISTICS", file=sys.stderr)
        print("=" * 60, file=sys.stderr)

        # Count by cookie name
        name_counts = {}
        for cookie in all_cookies:
            name = cookie["name"]
            name_counts[name] = name_counts.get(name, 0) + 1

        # Count by source file
        file_counts = {}
        for cookie in all_cookies:
            source = cookie["source_file"]
            file_counts[source] = file_counts.get(source, 0) + 1

        print(f"\nTotal cookies: {len(all_cookies):,}", file=sys.stderr)
        print(f"Unique cookie names: {len(name_counts)}", file=sys.stderr)
        print(f"Files with matches: {len(file_counts)}", file=sys.stderr)

        if args.realtime:
            print(f"Output directory: {args.results_dir}/{args.domain}/", file=sys.stderr)

        print("\nTop cookie names:", file=sys.stderr)
        for name, count in sorted(name_counts.items(), key=lambda x: -x[1])[:10]:
            print(f"  {name}: {count:,}", file=sys.stderr)

        if args.verbose:
            print("\nSample cookies:", file=sys.stderr)
            for cookie in all_cookies[:5]:
                print(f"  {cookie['name']}: {cookie['value'][:50]}...", file=sys.stderr)
                print(f"    Domain: {cookie['domain']}", file=sys.stderr)
                print(f"    Source: {cookie['source_file']}", file=sys.stderr)


if __name__ == "__main__":
    # Check if running with command line arguments
    if len(sys.argv) > 1:
        # Command line mode
        main()
    else:
        # Interactive menu mode
        try:
            main_menu()
        except KeyboardInterrupt:
            print("\n\n  ⚠️  Interrupted by user. Exiting...")
            sys.exit(0)
