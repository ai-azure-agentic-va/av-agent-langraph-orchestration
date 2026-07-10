Review changes (Add Summary, test singleton)
Add groups and test and mapping chnage and remove default
Jwt auth test
Intent fixes that are havign trouble
memory Leaks
Ai search tool improvement
Safety gate needs to be hardened instead of regex patter matching azure safety layer or other solutions can be explored but this will latency


#
	
Intent
	
Test Status


1
	
Get incidents for datasource X
	
Issue#1: Agent fails when user uses table names, conversational language, or keywords in the user query even when the keyword exists in the incident description. Looks like sub-agent is unable to fetch the details accurately. Sample test case: TC-008, TC-010, TC-015, TC-019


2
	
Summarize incident INC XXXX
	
Working as expected


3
	
Engineer lookup for datasource X
	
Issue#2: Works in fresh threads but fails in threads with long chat history. Sample: TC-031, TC-036
Issue#3: Agent resolves engineer-to-datasource mapping via configuration_item instead of description. TC - 035


4
	
Pipeline incidents for dataset X
	
Agent cannot distinguish pipeline infrastructure failures from data quality issues that reference a pipeline. Also returns closed incidents for open queries. Sample: TC-037, TC-040


5
	
Missing data incidents for datasource X
	
Failed to fetch missing data incidents. Sample: TC-047


6
	
Incidents last month by cause
	
Unable to fetch in one case, validate if there is an AND condition between filters. Sample: TC-53


7
	
Historical resolution for similar INC
	
Unable to fetch expected resolution notes from similar ticket. Sample: TC-042