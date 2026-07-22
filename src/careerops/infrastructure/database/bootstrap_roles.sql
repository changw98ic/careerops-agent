DO $roles$
DECLARE
    capability_role_name text;
    granted_role_name text;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'careerops_api') THEN
        CREATE ROLE careerops_api NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'careerops_retention') THEN
        CREATE ROLE careerops_retention NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'careerops_workflow') THEN
        CREATE ROLE careerops_workflow NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'careerops_outbox') THEN
        CREATE ROLE careerops_outbox NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'careerops_mailbox') THEN
        CREATE ROLE careerops_mailbox NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'careerops_mail_sender') THEN
        CREATE ROLE careerops_mail_sender NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'careerops_greenhouse_sender') THEN
        CREATE ROLE careerops_greenhouse_sender NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'careerops_side_effect') THEN
        CREATE ROLE careerops_side_effect NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'careerops_readonly') THEN
        CREATE ROLE careerops_readonly NOLOGIN;
    END IF;

    -- Creation is intentionally followed by unconditional hardening statements.
    -- This repairs capability roles that already existed with unsafe attributes.
    ALTER ROLE careerops_api
        WITH NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
    ALTER ROLE careerops_retention
        WITH NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
    ALTER ROLE careerops_workflow
        WITH NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
    ALTER ROLE careerops_outbox
        WITH NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
    ALTER ROLE careerops_mailbox
        WITH NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
    ALTER ROLE careerops_mail_sender
        WITH NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
    ALTER ROLE careerops_greenhouse_sender
        WITH NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
    ALTER ROLE careerops_side_effect
        WITH NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
    ALTER ROLE careerops_readonly
        WITH NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;

    -- Remove only memberships where a capability role is the member. Memberships
    -- granted *to* runtime identities remain intact, so those identities can still
    -- use SET ROLE without inheriting unrelated privileges.
    FOR capability_role_name, granted_role_name IN
        SELECT member_role.rolname, granted_role.rolname
        FROM pg_auth_members AS membership
        JOIN pg_roles AS member_role ON member_role.oid = membership.member
        JOIN pg_roles AS granted_role ON granted_role.oid = membership.roleid
        WHERE member_role.rolname IN (
            'careerops_api',
            'careerops_workflow',
            'careerops_retention',
            'careerops_outbox',
            'careerops_mailbox',
            'careerops_mail_sender',
            'careerops_greenhouse_sender',
            'careerops_side_effect',
            'careerops_readonly'
        )
    LOOP
        EXECUTE format(
            'REVOKE %I FROM %I',
            granted_role_name,
            capability_role_name
        );
    END LOOP;
END
$roles$;
