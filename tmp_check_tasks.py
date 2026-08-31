from cab_pipeline import extract_and_clean_cab
q = 'project = CAB AND issuetype in ("Tâche", "Task") ORDER BY created ASC'
df = extract_and_clean_cab(jql=q, verbose=False, max_tickets=100)
print('rows=', len(df))
print('types=', df['Type de ticket'].value_counts().to_dict())
print('sample=', df[['Clé de ticket', 'Type de ticket']].head(3).to_dict(orient='records'))
